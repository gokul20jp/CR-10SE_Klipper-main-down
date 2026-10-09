# layer_tracker.py
# Tracks per-layer timing, filament usage, and Z position for live monitoring.
#
# Exposes data via get_status() → automatically available to Moonraker/Fluidd
# via printer.objects.subscribe — no Moonraker modification needed.
#
# Add to printer.cfg:
#   [layer_tracker]
#
# Optional config:
#   layer_z_threshold: 0.05   # mm — Z must increase by this much to count as a new layer
#   max_layer_history: 500    # max layers to keep in memory
#
# Copyright (C) 2024 - CR-10 SE Klipper Fork
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import time

MAX_LAYER_HISTORY = 500
LAYER_Z_THRESHOLD = 0.05


class LayerTracker:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')

        self.layer_z_threshold = config.getfloat(
            'layer_z_threshold', LAYER_Z_THRESHOLD, minval=0.01, maxval=1.0)
        self.max_history = config.getint(
            'max_layer_history', MAX_LAYER_HISTORY, minval=10, maxval=5000)

        self._reset_state()

        self.printer.register_event_handler("klippy:connect", self._handle_connect)

        self.gcode.register_command(
            'LAYER_TRACKER_RESET',
            self.cmd_LAYER_TRACKER_RESET,
            desc="Reset layer tracker state")
        self.gcode.register_command(
            'LAYER_TRACKER_DUMP',
            self.cmd_LAYER_TRACKER_DUMP,
            desc="Dump layer history to console")

        logging.info("layer_tracker: initialized")

    def _reset_state(self):
        self.current_layer = 0
        self.total_layers = 0
        self.current_z = 0.0
        self.layer_start_time = None
        self.print_start_time = None
        self.last_layer_duration = 0.0
        self.last_layer_e_distance = 0.0
        self.layer_start_e = 0.0
        self.current_e = 0.0
        self.layer_history = []
        self.anomaly_events = []
        self.is_printing = False
        self.layer_durations = []

    def _handle_connect(self):
        try:
            self.toolhead = self.printer.lookup_object('toolhead')
        except Exception:
            self.toolhead = None
            logging.warning("layer_tracker: toolhead not found")

        try:
            self.print_stats = self.printer.lookup_object('print_stats')
        except Exception:
            self.print_stats = None
            logging.warning("layer_tracker: print_stats not found")

        reactor = self.printer.get_reactor()
        self._sample_timer = reactor.register_timer(
            self._sample_callback, reactor.NOW)

    def _sample_callback(self, eventtime):
        try:
            self._update(eventtime)
        except Exception as e:
            logging.error("layer_tracker: sample error: %s" % e)
        return eventtime + 0.25

    def _update(self, eventtime):
        if self.toolhead is None:
            return

        printing = False
        if self.print_stats is not None:
            state = self.print_stats.get_status(eventtime).get('state', '')
            printing = (state == 'printing')
            info = self.print_stats.get_status(eventtime).get('info', {})
            total = info.get('total_layer', 0)
            if total and total != self.total_layers:
                self.total_layers = total

        if printing and not self.is_printing:
            self._on_print_start(eventtime)
        if not printing and self.is_printing:
            self._on_print_end(eventtime)

        self.is_printing = printing

        if not printing:
            return

        pos = self.toolhead.get_position()
        current_z = pos[2]
        current_e = pos[3]
        self.current_e = current_e

        if (current_z > self.current_z + self.layer_z_threshold and
                current_z > 0.01):

            if self.current_layer > 0 and self.layer_start_time is not None:
                self._on_layer_complete(eventtime, current_z, current_e)

            self.current_layer += 1
            self.current_z = current_z
            self.layer_start_time = eventtime
            self.layer_start_e = current_e
        else:
            self.current_z = max(self.current_z, current_z)

    def _on_print_start(self, eventtime):
        self._reset_state()
        self.is_printing = True
        self.print_start_time = eventtime
        self.current_layer = 1
        self.layer_start_time = eventtime
        if self.toolhead:
            pos = self.toolhead.get_position()
            self.current_z = pos[2]
            self.layer_start_e = pos[3]
        self.printer.send_event("layer_tracker:print_start", eventtime)
        logging.info("layer_tracker: print started")

    def _on_print_end(self, eventtime):
        if self.current_layer > 0 and self.layer_start_time is not None:
            duration = eventtime - self.layer_start_time
            e_dist = self.current_e - self.layer_start_e
            self._record_layer(self.current_layer, self.current_z, duration, e_dist)
        self.printer.send_event("layer_tracker:print_end", eventtime)
        logging.info("layer_tracker: print ended, %d layers" % self.current_layer)

    def _on_layer_complete(self, eventtime, new_z, current_e):
        duration = eventtime - self.layer_start_time
        e_dist = current_e - self.layer_start_e
        self.last_layer_duration = duration
        self.last_layer_e_distance = e_dist
        self._record_layer(self.current_layer, self.current_z, duration, e_dist)
        self._check_layer_anomaly(self.current_layer, duration, e_dist)

    def _record_layer(self, layer_num, z, duration, e_dist):
        self.layer_durations.append(duration)
        if len(self.layer_durations) > 20:
            self.layer_durations.pop(0)

        entry = {
            'layer': layer_num,
            'z': round(z, 3),
            'duration': round(duration, 2),
            'e_distance': round(e_dist, 3),
            'timestamp': time.strftime("%Y-%m-%dT%H:%M:%S")
        }
        self.layer_history.append(entry)
        if len(self.layer_history) > self.max_history:
            self.layer_history.pop(0)

    def _check_layer_anomaly(self, layer_num, duration, e_dist):
        if len(self.layer_durations) < 5:
            return

        avg = sum(self.layer_durations[:-1]) / len(self.layer_durations[:-1])
        if avg < 0.1:
            return

        ratio = duration / avg
        severity = None
        if ratio > 3.0:
            severity = 'critical'
        elif ratio > 1.8:
            severity = 'warning'

        if severity:
            event = {
                'type': 'slow_layer',
                'severity': severity,
                'layer': layer_num,
                'duration': round(duration, 2),
                'avg_duration': round(avg, 2),
                'ratio': round(ratio, 2),
                'timestamp': time.strftime("%Y-%m-%dT%H:%M:%S")
            }
            self.anomaly_events.append(event)
            if len(self.anomaly_events) > 100:
                self.anomaly_events.pop(0)

            msg = ("layer_tracker: %s layer %d took %.1fs (%.1fx avg=%.1fs)"
                   % (severity.upper(), layer_num, duration, ratio, avg))
            logging.warning(msg)
            try:
                self.gcode.respond_info(msg)
            except Exception:
                pass

    def get_status(self, eventtime):
        avg_duration = 0.0
        if self.layer_durations:
            avg_duration = sum(self.layer_durations) / len(self.layer_durations)

        elapsed = 0.0
        if self.print_start_time is not None and self.is_printing:
            elapsed = eventtime - self.print_start_time

        layer_elapsed = 0.0
        if self.layer_start_time is not None and self.is_printing:
            layer_elapsed = eventtime - self.layer_start_time

        return {
            'is_printing': self.is_printing,
            'current_layer': self.current_layer,
            'total_layers': self.total_layers,
            'current_z': round(self.current_z, 3),
            'last_layer_duration': round(self.last_layer_duration, 2),
            'last_layer_e_distance': round(self.last_layer_e_distance, 3),
            'layer_elapsed': round(layer_elapsed, 1),
            'avg_layer_duration': round(avg_duration, 2),
            'print_elapsed': round(elapsed, 1),
            'layer_history': self.layer_history[-50:],
            'anomaly_events': self.anomaly_events[-20:]
        }

    def cmd_LAYER_TRACKER_RESET(self, gcmd):
        self._reset_state()
        gcmd.respond_info("layer_tracker: state reset")

    def cmd_LAYER_TRACKER_DUMP(self, gcmd):
        if not self.layer_history:
            gcmd.respond_info("layer_tracker: no layer history")
            return
        lines = ["layer_tracker: layer history (last %d)" % len(self.layer_history)]
        for entry in self.layer_history[-20:]:
            lines.append("  Layer %3d | Z=%.3fmm | %.2fs | E=%.1fmm"
                         % (entry['layer'], entry['z'], entry['duration'], entry['e_distance']))
        gcmd.respond_info("\n".join(lines))


def load_config(config):
    return LayerTracker(config)
