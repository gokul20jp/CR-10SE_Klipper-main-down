# load_cell_monitor.py
# Reference-only Z-offset calibration data logger using prtouch_v2 pressure sensor.
#
# Hooks into Z_OFFSET_CALIBRATION events to record:
#   - Z position at trigger
#   - ADC value at trigger (via prtouch_v2 status)
#   - Nozzle + bed temperature
#   - Multiple samples per session
#   - Multiple bed positions per session
#
# Data saved to JSON for display in Fluidd UI (reference only — does NOT modify z_offset).
#
# Copyright (C) 2024 - CR-10 SE Klipper Fork
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import json
import os
import time
import math

DATA_FILE = "/usr/data/printer_data/config/load_cell_data.json"
MAX_SESSIONS = 200

class LoadCellMonitor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.data_file = config.get('data_file', DATA_FILE)
        self.max_sessions = config.getint('max_sessions', MAX_SESSIONS, minval=10, maxval=5000)

        # State for active session
        self._active_session = None
        self._active_position = None

        # Register GCode commands
        self.gcode.register_command(
            'LOAD_CELL_SESSION_START',
            self.cmd_LOAD_CELL_SESSION_START,
            desc="Start a new load cell reference calibration session")
        self.gcode.register_command(
            'LOAD_CELL_POSITION_START',
            self.cmd_LOAD_CELL_POSITION_START,
            desc="Start recording samples at a new position")
        self.gcode.register_command(
            'LOAD_CELL_RECORD_SAMPLE',
            self.cmd_LOAD_CELL_RECORD_SAMPLE,
            desc="Record one sample: Z position + ADC value")
        self.gcode.register_command(
            'LOAD_CELL_POSITION_END',
            self.cmd_LOAD_CELL_POSITION_END,
            desc="Finalize current position samples")
        self.gcode.register_command(
            'LOAD_CELL_SESSION_END',
            self.cmd_LOAD_CELL_SESSION_END,
            desc="Finalize and save the current session to JSON")
        self.gcode.register_command(
            'LOAD_CELL_CLEAR',
            self.cmd_LOAD_CELL_CLEAR,
            desc="Clear all load cell history")
        self.gcode.register_command(
            'LOAD_CELL_DUMP',
            self.cmd_LOAD_CELL_DUMP,
            desc="Print last session stats to console")

        logging.info("load_cell_monitor: initialized, logging to %s" % self.data_file)

    # ── Temperature helpers ───────────────────────────────────────────────────

    def _get_temperatures(self):
        temps = {}
        try:
            reactor = self.printer.get_reactor()
            extruder = self.printer.lookup_object('extruder', None)
            if extruder:
                status = extruder.get_status(reactor.monotonic())
                temps['nozzle_temp'] = round(status.get('temperature', 0), 2)
                temps['nozzle_target'] = round(status.get('target', 0), 2)
        except Exception:
            pass
        try:
            reactor = self.printer.get_reactor()
            heater_bed = self.printer.lookup_object('heater_bed', None)
            if heater_bed:
                status = heater_bed.get_status(reactor.monotonic())
                temps['bed_temp'] = round(status.get('temperature', 0), 2)
                temps['bed_target'] = round(status.get('target', 0), 2)
        except Exception:
            pass
        return temps

    # ── JSON persistence ──────────────────────────────────────────────────────

    def _read_data(self):
        if not os.path.exists(self.data_file):
            return []
        try:
            with open(self.data_file, 'r') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as e:
            logging.error("load_cell_monitor: failed to read data: %s" % e)
            return []

    def _write_data(self, sessions):
        if len(sessions) > self.max_sessions:
            sessions = sessions[-self.max_sessions:]
        try:
            os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
            tmp = self.data_file + ".tmp"
            with open(tmp, 'w') as f:
                json.dump(sessions, f, indent=2)
            os.rename(tmp, self.data_file)
        except Exception as e:
            logging.error("load_cell_monitor: failed to write data: %s" % e)

    # ── Statistics ────────────────────────────────────────────────────────────

    def _compute_stats(self, samples):
        if not samples:
            return {}
        z_values = [s['z_at_trigger'] for s in samples]
        adc_values = [s.get('adc_at_trigger', 0) for s in samples]
        z_mean = sum(z_values) / len(z_values)
        z_stddev = math.sqrt(sum((v - z_mean) ** 2 for v in z_values) / len(z_values))
        return {
            'z_mean': round(z_mean, 6),
            'z_stddev': round(z_stddev, 6),
            'z_min': round(min(z_values), 6),
            'z_max': round(max(z_values), 6),
            'adc_mean': round(sum(adc_values) / len(adc_values), 1) if adc_values else 0,
            'sample_count': len(samples)
        }

    # ── GCode commands ────────────────────────────────────────────────────────

    def cmd_LOAD_CELL_SESSION_START(self, gcmd):
        """Start a new calibration session."""
        note = gcmd.get('NOTE', '')
        temps = self._get_temperatures()
        self._active_session = {
            'session_id': time.strftime("%Y-%m-%dT%H:%M:%S"),
            'timestamp': time.strftime("%Y-%m-%dT%H:%M:%S"),
            'note': note,
            'positions': [],
            **temps
        }
        self._active_position = None
        gcmd.respond_info(
            "load_cell_monitor: session started at %s (nozzle=%.1f°C bed=%.1f°C)"
            % (self._active_session['session_id'],
               temps.get('nozzle_temp', 0),
               temps.get('bed_temp', 0)))

    def cmd_LOAD_CELL_POSITION_START(self, gcmd):
        """Start collecting samples at a specific XY position."""
        if self._active_session is None:
            gcmd.error("load_cell_monitor: no active session — call LOAD_CELL_SESSION_START first")
            return
        label = gcmd.get('LABEL', 'pos_%d' % (len(self._active_session['positions']) + 1))
        x = gcmd.get_float('X', 110.0)
        y = gcmd.get_float('Y', 110.0)
        self._active_position = {
            'pos_label': label,
            'x': x,
            'y': y,
            'samples': []
        }
        gcmd.respond_info(
            "load_cell_monitor: recording at %s (X=%.1f, Y=%.1f)"
            % (label, x, y))

    def cmd_LOAD_CELL_RECORD_SAMPLE(self, gcmd):
        """Record one Z+ADC sample at the current position."""
        if self._active_position is None:
            gcmd.error("load_cell_monitor: no active position — call LOAD_CELL_POSITION_START first")
            return
        z = gcmd.get_float('Z')
        adc = gcmd.get_int('ADC', 0)
        sample_num = len(self._active_position['samples']) + 1
        self._active_position['samples'].append({
            'sample_num': sample_num,
            'z_at_trigger': round(z, 6),
            'adc_at_trigger': adc
        })
        gcmd.respond_info(
            "load_cell_monitor: sample %d — Z=%.6f mm, ADC=%d"
            % (sample_num, z, adc))

    def cmd_LOAD_CELL_POSITION_END(self, gcmd):
        """Finalize samples at current position and compute stats."""
        if self._active_position is None:
            gcmd.error("load_cell_monitor: no active position")
            return
        stats = self._compute_stats(self._active_position['samples'])
        self._active_position['stats'] = stats
        self._active_session['positions'].append(self._active_position)
        gcmd.respond_info(
            "load_cell_monitor: position '%s' done — z_mean=%.4fmm stddev=%.4fmm"
            % (self._active_position['pos_label'],
               stats.get('z_mean', 0),
               stats.get('z_stddev', 0)))
        self._active_position = None

    def cmd_LOAD_CELL_SESSION_END(self, gcmd):
        """Finalize session, compute overall stats, save to JSON."""
        if self._active_session is None:
            gcmd.error("load_cell_monitor: no active session")
            return
        if self._active_position is not None:
            # Auto-close position if not closed
            self.cmd_LOAD_CELL_POSITION_END(gcmd)

        # Compute overall z statistics across all positions
        all_z_means = [p['stats']['z_mean'] for p in self._active_session['positions']
                       if 'stats' in p]
        if all_z_means:
            overall_mean = sum(all_z_means) / len(all_z_means)
            overall_spread = max(all_z_means) - min(all_z_means)
            self._active_session['overall_stats'] = {
                'z_mean': round(overall_mean, 6),
                'z_spread_across_positions': round(overall_spread, 6),
                'position_count': len(self._active_session['positions']),
                'recommended_z_offset': round(overall_mean, 4)
            }

        sessions = self._read_data()
        sessions.append(self._active_session)
        self._write_data(sessions)

        gcmd.respond_info(
            "load_cell_monitor: session saved — z_mean=%.4fmm across %d positions. "
            "Reference z_offset: %.4fmm. NOT applied automatically."
            % (self._active_session['overall_stats'].get('z_mean', 0),
               len(self._active_session['positions']),
               self._active_session['overall_stats'].get('recommended_z_offset', 0)))
        self._active_session = None
        self._active_position = None

    def cmd_LOAD_CELL_CLEAR(self, gcmd):
        """Clear all history."""
        self._write_data([])
        gcmd.respond_info("load_cell_monitor: history cleared")

    def cmd_LOAD_CELL_DUMP(self, gcmd):
        """Print last session to console."""
        sessions = self._read_data()
        if not sessions:
            gcmd.respond_info("load_cell_monitor: no data")
            return
        last = sessions[-1]
        lines = ["load_cell_monitor: last session %s" % last.get('session_id', '')]
        lines.append("  nozzle=%.1f°C bed=%.1f°C"
                     % (last.get('nozzle_temp', 0), last.get('bed_temp', 0)))
        for pos in last.get('positions', []):
            stats = pos.get('stats', {})
            lines.append("  %s (%.0f,%.0f): z_mean=%.4fmm ±%.4fmm"
                         % (pos['pos_label'], pos['x'], pos['y'],
                            stats.get('z_mean', 0), stats.get('z_stddev', 0)))
        ov = last.get('overall_stats', {})
        lines.append("  Overall: z_mean=%.4fmm spread=%.4fmm → reference z_offset=%.4fmm"
                     % (ov.get('z_mean', 0), ov.get('z_spread_across_positions', 0),
                        ov.get('recommended_z_offset', 0)))
        gcmd.respond_info("\n".join(lines))

    def get_status(self, eventtime):
        """Expose last session reference z_offset to Moonraker/Fluidd."""
        sessions = self._read_data()
        last = sessions[-1] if sessions else {}
        ov = last.get('overall_stats', {})
        return {
            'session_count': len(sessions),
            'last_session_id': last.get('session_id'),
            'last_nozzle_temp': last.get('nozzle_temp'),
            'last_bed_temp': last.get('bed_temp'),
            'last_z_mean': ov.get('z_mean'),
            'last_recommended_z_offset': ov.get('recommended_z_offset'),
            'last_z_spread': ov.get('z_spread_across_positions'),
            'is_recording': self._active_session is not None
        }


def load_config(config):
    return LoadCellMonitor(config)
