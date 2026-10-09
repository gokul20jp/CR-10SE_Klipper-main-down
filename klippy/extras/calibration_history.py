# calibration_history.py
# Logs z_offset and bed mesh calibration history with timestamps
# to a JSON file for display in Fluidd or external tools.
#
# Copyright (C) 2024 - CR-10 SE Klipper Fork
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging, json, os, time

HISTORY_FILE = "/usr/data/printer_data/config/calibration_history.json"
MAX_HISTORY_ENTRIES = 1000  # Keep last 100 entries to avoid unbounded growth

class CalibrationHistory:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.history_file = config.get('history_file', HISTORY_FILE)
        self.max_entries = config.getint('max_entries', MAX_HISTORY_ENTRIES,
                                         minval=10, maxval=1000)
        self.gcode = self.printer.lookup_object('gcode')

        # In-memory cache for get_status() — avoids reading and parsing the
        # full JSON file on every ~1 Hz Moonraker poll.
        # Invalidated (set to None) whenever _write_history() is called.
        self._history_cache = None          # list[dict] | None
        self._history_cache_mtime = None    # float | None — file mtime at last load

        # Register GCode commands
        self.gcode.register_command(
            'CALIBRATION_HISTORY_LOG',
            self.cmd_CALIBRATION_HISTORY_LOG,
            desc=self.cmd_CALIBRATION_HISTORY_LOG_help)
        self.gcode.register_command(
            'CALIBRATION_HISTORY_CLEAR',
            self.cmd_CALIBRATION_HISTORY_CLEAR,
            desc=self.cmd_CALIBRATION_HISTORY_CLEAR_help)

        # Register event handlers
        self.printer.register_event_handler(
            "klippy:connect", self._handle_connect)

        logging.info("calibration_history: initialized, logging to %s"
                     % self.history_file)

    def _handle_connect(self):
        # Register Moonraker/Fluidd webhook endpoints
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.register_endpoint(
                "calibration_history/list",
                self._handle_webhook_list)
            webhooks.register_endpoint(
                "calibration_history/clear",
                self._handle_webhook_clear)
            logging.info("calibration_history: registered webhook endpoints")
        except Exception as e:
            logging.error("calibration_history: failed to register webhooks: %s" % e)

        # Hook into bed_mesh ProfileManager.save_profile
        # and configfile.set for z_offset tracking
        # by monkey-patching the configfile module's set() method
        try:
            configfile = self.printer.lookup_object('configfile')
            original_set = configfile.set

            def patched_set(section, option, value):
                original_set(section, option, value)
                # Intercept z_offset changes for bltouch or prtouch_v2
                if option == 'z_offset' and section.lower() in (
                        'bltouch', 'prtouch_v2', 'probe'):
                    self._log_zoffset_change(section, float(value))

            configfile.set = patched_set
            logging.info("calibration_history: hooked into configfile.set")
        except Exception as e:
            logging.error("calibration_history: failed to hook configfile.set: %s" % e)

        # Hook into bed_mesh ProfileManager.save_profile
        try:
            bedmesh = self.printer.lookup_object('bed_mesh', None)
            if bedmesh is not None:
                pmgr = bedmesh.pmgr
                original_save = pmgr.save_profile

                def patched_save_profile(prof_name):
                    original_save(prof_name)
                    self._log_mesh_save(prof_name, bedmesh)

                pmgr.save_profile = patched_save_profile
                bedmesh.save_profile = patched_save_profile
                logging.info("calibration_history: hooked into bed_mesh.save_profile")
        except Exception as e:
            logging.error("calibration_history: failed to hook bed_mesh: %s" % e)

    def _read_history(self):
        """Return the history list, served from an in-memory cache when possible.

        The cache is considered valid when the file's mtime has not changed since
        the last load.  This avoids reading and JSON-parsing the full history file
        on every ~1 Hz Moonraker get_status() poll while still picking up any
        external (out-of-process) edits to the file.
        """
        try:
            if os.path.exists(self.history_file):
                mtime = os.path.getmtime(self.history_file)
                if (self._history_cache is not None
                        and self._history_cache_mtime == mtime):
                    return self._history_cache
                # Cache miss — file is new or has changed; reload it.
                with open(self.history_file, 'r') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self._history_cache = data
                    self._history_cache_mtime = mtime
                    return self._history_cache
            else:
                # File does not exist yet; cache an empty list so subsequent
                # read calls don't hit the filesystem repeatedly.
                self._history_cache = []
                self._history_cache_mtime = None
                return self._history_cache
        except Exception as e:
            logging.error("calibration_history: failed to read history: %s" % e)
        return []

    def _write_history(self, history):
        """Write history to JSON file and refresh the in-memory cache."""
        # Trim to max entries
        if len(history) > self.max_entries:
            history = history[-self.max_entries:]
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
            tmp_file = self.history_file + ".tmp"
            with open(tmp_file, 'w') as f:
                json.dump(history, f, indent=2)
            os.rename(tmp_file, self.history_file)
            # Update the cache immediately so the very next get_status() call
            # (which arrives within ~1 s) never has to re-read the file.
            self._history_cache = history
            self._history_cache_mtime = os.path.getmtime(self.history_file)
            logging.info("calibration_history: wrote %d entries to %s"
                         % (len(history), self.history_file))
        except Exception as e:
            logging.error("calibration_history: failed to write history: %s" % e)

    def _get_timestamp(self):
        """Return ISO 8601 timestamp string."""
        return time.strftime("%Y-%m-%dT%H:%M:%S")

    def _get_temperatures(self):
        """
        Read current bed and nozzle temperatures from Klipper.
        Returns a dict with actual and target temps for both heaters.
        This is stored alongside every calibration event so we can correlate
        Z-offset and mesh changes with thermal expansion / temperature conditions.
        """
        temps = {}
        try:
            extruder = self.printer.lookup_object('extruder', None)
            if extruder is not None:
                heater = extruder.get_heater()
                current, target = heater.get_temp(self.printer.get_reactor().monotonic())
                temps['nozzle_temp'] = round(current, 2)
                temps['nozzle_target'] = round(target, 2)
        except Exception:
            try:
                # Fallback: read from printer status dict
                reactor = self.printer.get_reactor()
                extruder_obj = self.printer.lookup_object('extruder', None)
                if extruder_obj:
                    status = extruder_obj.get_status(reactor.monotonic())
                    temps['nozzle_temp'] = round(status.get('temperature', 0), 2)
                    temps['nozzle_target'] = round(status.get('target', 0), 2)
            except Exception:
                pass

        try:
            heater_bed = self.printer.lookup_object('heater_bed', None)
            if heater_bed is not None:
                reactor = self.printer.get_reactor()
                current, target = heater_bed.get_temp(reactor.monotonic())
                temps['bed_temp'] = round(current, 2)
                temps['bed_target'] = round(target, 2)
        except Exception:
            try:
                heater_bed = self.printer.lookup_object('heater_bed', None)
                if heater_bed:
                    reactor = self.printer.get_reactor()
                    status = heater_bed.get_status(reactor.monotonic())
                    temps['bed_temp'] = round(status.get('temperature', 0), 2)
                    temps['bed_target'] = round(status.get('target', 0), 2)
            except Exception:
                pass

        return temps

    def _log_zoffset_change(self, section, z_offset_value):
        """Log a z_offset change event and check for drift."""
        history = self._read_history()
        entry = {
            "type": "z_offset",
            "timestamp": self._get_timestamp(),
            "section": section,
            "z_offset": round(z_offset_value, 6),
            "trigger": "configfile.set"
        }
        # Capture current temperatures — allows correlating Z-offset drift
        # with thermal expansion (bed heating causes bed expansion → z_offset changes)
        entry.update(self._get_temperatures())
        history.append(entry)
        self._write_history(history)
        logging.info("calibration_history: logged z_offset=%.6f for [%s]"
                     % (z_offset_value, section))

        # ---- DRIFT DETECTION (log only, no action) ----
        # Collect last 5 z_offset values from history
        try:
            past_offsets = [
                e['z_offset'] for e in history
                if e.get('type') == 'z_offset'
                and e.get('section', '').lower() == section.lower()
            ][-5:]  # last 5 entries for this section

            if len(past_offsets) >= 3:
                drift = max(past_offsets) - min(past_offsets)
                DRIFT_WARN_THRESHOLD = 0.05  # mm — warn if last 5 span > 0.05mm
                if drift > DRIFT_WARN_THRESHOLD:
                    msg = (
                        "calibration_history: WARNING z_offset DRIFT detected! "
                        "Last %d values for [%s]: %s  |  range=%.4fmm (threshold=%.2fmm). "
                        "Consider recleaning nozzle or checking pressure sensor."
                        % (len(past_offsets), section,
                           [round(v, 4) for v in past_offsets],
                           drift, DRIFT_WARN_THRESHOLD)
                    )
                    logging.warning(msg)
                    try:
                        # Also print to Klipper console so it's visible in Fluidd
                        gcode = self.printer.lookup_object('gcode')
                        gcode.respond_info(msg)
                    except Exception:
                        pass
        except Exception as e:
            logging.error("calibration_history: drift check failed: %s" % e)

    def _log_mesh_save(self, prof_name, bedmesh):
        """Log a mesh save event with full mesh data."""
        try:
            z_mesh = bedmesh.get_mesh()
            if z_mesh is None:
                return

            probed_matrix = z_mesh.get_probed_matrix()
            mesh_params = z_mesh.get_mesh_params()

            # Compute mesh stats
            all_vals = [v for row in probed_matrix for v in row]
            mesh_min = round(min(all_vals), 6) if all_vals else 0.0
            mesh_max = round(max(all_vals), 6) if all_vals else 0.0
            mesh_avg = round(sum(all_vals) / len(all_vals), 6) if all_vals else 0.0
            mesh_range = round(mesh_max - mesh_min, 6)

            # Get current z_offset from probe
            z_offset_val = None
            try:
                probe = self.printer.lookup_object('probe', None)
                if probe is not None:
                    z_offset_val = round(probe.get_offsets()[2], 6)
            except Exception:
                pass

            history = self._read_history()
            entry = {
                "type": "mesh_save",
                "timestamp": self._get_timestamp(),
                "profile": prof_name,
                "z_offset_at_save": z_offset_val,
                "mesh_stats": {
                    "min": mesh_min,
                    "max": mesh_max,
                    "avg": mesh_avg,
                    "range": mesh_range,
                    "x_count": mesh_params.get('x_count', 0),
                    "y_count": mesh_params.get('y_count', 0),
                    "min_x": round(mesh_params.get('min_x', 0), 2),
                    "max_x": round(mesh_params.get('max_x', 0), 2),
                    "min_y": round(mesh_params.get('min_y', 0), 2),
                    "max_y": round(mesh_params.get('max_y', 0), 2),
                    "algo": mesh_params.get('algo', 'unknown'),
                    "tension": mesh_params.get('tension', 0)
                },
                "probed_matrix": probed_matrix,
                "trigger": "save_profile"
            }
            # Capture temperatures at time of mesh save — key for understanding
            # how bed thermal expansion affects mesh shape across different temps
            entry.update(self._get_temperatures())
            history.append(entry)
            self._write_history(history)
            logging.info(
                "calibration_history: logged mesh save profile=[%s] "
                "min=%.4f max=%.4f range=%.4f z_offset=%s"
                % (prof_name, mesh_min, mesh_max, mesh_range,
                   str(z_offset_val)))
        except Exception as e:
            logging.error("calibration_history: failed to log mesh: %s" % e)

    cmd_CALIBRATION_HISTORY_LOG_help = (
        "Manually log a calibration history entry with a note")
    def cmd_CALIBRATION_HISTORY_LOG(self, gcmd):
        """Manually log a snapshot with optional note."""
        note = gcmd.get('NOTE', '')
        trigger = gcmd.get('TRIGGER', 'manual')

        entry = {
            "type": "manual_snapshot",
            "timestamp": self._get_timestamp(),
            "note": note,
            "trigger": trigger
        }

        # Get z_offset
        try:
            probe = self.printer.lookup_object('probe', None)
            if probe is not None:
                entry['z_offset'] = round(probe.get_offsets()[2], 6)
        except Exception:
            pass

        # Get mesh
        try:
            bedmesh = self.printer.lookup_object('bed_mesh', None)
            if bedmesh is not None:
                z_mesh = bedmesh.get_mesh()
                if z_mesh is not None:
                    probed_matrix = z_mesh.get_probed_matrix()
                    all_vals = [v for row in probed_matrix for v in row]
                    if all_vals:
                        entry['mesh_stats'] = {
                            "min": round(min(all_vals), 6),
                            "max": round(max(all_vals), 6),
                            "avg": round(sum(all_vals) / len(all_vals), 6),
                            "range": round(max(all_vals) - min(all_vals), 6)
                        }
                        entry['probed_matrix'] = probed_matrix
        except Exception:
            pass

        # Capture temperatures at time of manual snapshot
        entry.update(self._get_temperatures())
        history = self._read_history()
        history.append(entry)
        self._write_history(history)
        gcmd.respond_info(
            "calibration_history: logged manual snapshot at %s (nozzle=%.1f°C bed=%.1f°C)"
            % (entry['timestamp'],
               entry.get('nozzle_temp', 0),
               entry.get('bed_temp', 0)))

    cmd_CALIBRATION_HISTORY_CLEAR_help = "Clear all calibration history"
    def cmd_CALIBRATION_HISTORY_CLEAR(self, gcmd):
        """Clear history file."""
        self._write_history([])
        gcmd.respond_info(
            "calibration_history: history cleared at %s"
            % self._get_timestamp())

    # -------------------------------------------------------------------------
    # Webhook handlers — accessible via Moonraker HTTP API
    # and therefore Fluidd panels / custom macros
    # -------------------------------------------------------------------------

    def _handle_webhook_list(self, web_request):
        """
        GET /printer/calibration_history/list
        Optional query params:
          ?limit=N     — return last N entries (default 50)
          ?type=...    — filter by entry type (z_offset, mesh_save, manual_snapshot)
        """
        try:
            limit = web_request.get_int('limit', 50)
        except Exception:
            limit = 50
        try:
            filter_type = web_request.get_str('type', None)
        except Exception:
            filter_type = None

        all_history = self._read_history()
        total = len(all_history)
        if filter_type:
            filtered = [e for e in all_history if e.get('type') == filter_type]
        else:
            filtered = all_history
        returned = filtered[-limit:]

        web_request.send({
            'history': returned,
            'total': total,
            'returned': len(returned)
        })

    def _handle_webhook_clear(self, web_request):
        """
        POST /printer/calibration_history/clear
        Clears all history entries.
        """
        self._write_history([])
        web_request.send({
            'result': 'ok',
            'timestamp': self._get_timestamp()
        })

    def get_status(self, eventtime):
        """Return status for Fluidd/Moonraker access.

        Called ~1 Hz by Moonraker.  _read_history() is served from the
        in-memory cache (O(1) — no file I/O) unless the file has been
        modified externally since the last write.
        """
        history = self._read_history()
        recent = history[-5:] if len(history) >= 5 else history
        last_zoffset = None
        last_mesh_range = None
        total = len(history)
        for entry in reversed(history):
            if entry.get('type') == 'z_offset' and last_zoffset is None:
                last_zoffset = entry.get('z_offset')
            if entry.get('type') == 'mesh_save' and last_mesh_range is None:
                stats = entry.get('mesh_stats', {})
                last_mesh_range = stats.get('range')
            if last_zoffset is not None and last_mesh_range is not None:
                break
        return {
            'history_file': self.history_file,
            'total_entries': total,
            'last_z_offset': last_zoffset,
            'last_mesh_range': last_mesh_range,
            'recent_entries': recent
        }


def load_config(config):
    return CalibrationHistory(config)
