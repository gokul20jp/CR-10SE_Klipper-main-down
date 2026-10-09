# prtouch support
#
# Copyright (C) 2018-2021  Creality <wangyulong878@sina.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import traceback
from . import probe
from . import prtouch_v2_wrapper


def _get_caller_context(depth=4):
    """Walk up the call stack to find who invoked run_step_prtouch.

    Returns a short string like 'run_G29_Z:L1860' or 'probe_ready:L1113'.
    Skips frames inside this wrapper module itself.
    """
    stack = traceback.extract_stack()
    # stack[-1] is this function, [-2] is _wrapped_run_step_prtouch,
    # we want the frame that called _wrapped_run_step_prtouch and above.
    callers = []
    for frame in reversed(stack[:-2]):
        # Stop after collecting enough context
        if len(callers) >= depth:
            break
        # Skip frames from this file (prtouch_v2.py) — we want the .so caller
        if 'prtouch_v2.py' in frame.filename and 'prtouch_v2_wrapper' not in frame.filename:
            continue
        callers.append('%s:%s:L%d' % (
            frame.filename.rsplit('/', 1)[-1], frame.name, frame.lineno))
    return ' <- '.join(callers) if callers else 'unknown'


def load_config(config):
    vrt = prtouch_v2_wrapper.PRTouchEndstopWrapper(config)

    #TODO: Please find the overrided functions below - Overrided the functions presnet in .so file.

    # z_probe_correction: systematic offset between PRTouch trigger point and
    # actual ideal printing contact. The binary's run_step_prtouch returns a
    # value that consistently underestimates the required z_offset correction.
    # Subtracting this from the returned out_mm makes the result more negative,
    # causing the binary to apply a larger z_offset correction.
    # Tune by: (desired_z_offset - calibrated_z_offset). Default 0 = no change.
    z_probe_correction = config.getfloat('z_probe_correction', default=0.0,
                                         minval=-1.0, maxval=1.0)

    # log_probe_calls: when True, logs every run_step_prtouch call to klippy.log
    # with full context: caller, XY position, mesh index, named parameters, and
    # return values. Used to discover the calling convention of the CR-10 SE .so
    # binary so we can build automated mesh verification.
    # SAFE: logging is read-only — does not modify z_offset or trigger saves.
    # Set to False once the calling convention is confirmed. For now, it can be true. 
    log_probe_calls = config.getboolean('log_probe_calls', default=True)

    _orig_run_step_prtouch = vrt.run_step_prtouch

    def _wrapped_run_step_prtouch(*args, **kwargs):
        if log_probe_calls:
            # --- Capture caller context ---
            caller = _get_caller_context()

            # --- Extract named parameters from positional args ---
            # Reference signature (from prtouch_v2_wrapper_cr10se.py):
            #   run_step_prtouch(self, down_min_z, probe_min_3err,
            #                    rt_last=False, pro_cnt=3, crt_cnt=3,
            #                    fast_probe=False, re_g28=False)
            param_names = ('self', 'down_min_z', 'probe_min_3err',
                           'rt_last', 'pro_cnt', 'crt_cnt',
                           'fast_probe', 're_g28')
            named_args = {}
            for i, val in enumerate(args):
                if i < len(param_names) and param_names[i] != 'self':
                    named_args[param_names[i]] = val
            named_args.update(kwargs)

            # --- Capture toolhead XY position and mesh index ---
            try:
                pos = vrt.toolhead.get_position()
                xy_str = 'X=%.3f Y=%.3f Z=%.3f' % (pos[0], pos[1], pos[2])
            except Exception:
                xy_str = 'X=? Y=? Z=?'

            try:
                mesh_idx = getattr(vrt, 'g29_cnt', '?')
            except Exception:
                mesh_idx = '?'

            logging.info(
                "prtouch_v2: >>> run_step_prtouch CALLED\n"
                "  caller:     %s\n"
                "  position:   %s\n"
                "  mesh_index: %s\n"
                "  params:     %s\n"
                "  raw_args:   len=%d, raw_kwargs=%s"
                % (caller, xy_str, mesh_idx, named_args,
                   len(args), kwargs))

        out_mm, flag = _orig_run_step_prtouch(*args, **kwargs)

        if log_probe_calls:
            logging.info(
                "prtouch_v2: <<< run_step_prtouch RESULT\n"
                "  out_mm=%.6f  flag=%s  (before correction)" % (out_mm, flag))

        if flag and z_probe_correction != 0.0:
            out_mm -= z_probe_correction

        if log_probe_calls and z_probe_correction != 0.0:
            logging.info(
                "prtouch_v2:     run_step_prtouch CORRECTED\n"
                "  out_mm=%.6f  (z_probe_correction=%.4f applied)"
                % (out_mm, z_probe_correction))

        return out_mm, flag

    vrt.run_step_prtouch = _wrapped_run_step_prtouch

    # config.get_printer().add_object('probe', probe.PrinterProbe(config, vrt))
    return vrt


# /home/cc/moonraker-env/bin/python3.10 /home/cc/moonraker/moonraker/moonraker.py -d /home/cc/printer_data
# sudo service klipper stop
# /home/cc/klippy-env/bin/python3.10 /home/cc/klipper/klippy/klippy.py /home/cc/printer_data/config/printer.cfg -a /home/cc/printer_data/comms/klippy.sock -l /home/cc/printer_data/logs/klippy.log

# ./micropython /home/cc/klipper/klippy/klippy.py /home/cc/printer_data/config/printer.cfg -a /home/cc/printer_data/comms/klippy.sock -l /home/cc/printer_data/logs/klippy.log

# /home/cc/klippy-env/bin/python3.10 /home/cc/micropython_test/klipper/klippy/klippy.py /home/cc/printer_data/config/printer.cfg -a /home/cc/printer_data/comms/klippy.sock -l /home/cc/printer_data/logs/klippy.log
