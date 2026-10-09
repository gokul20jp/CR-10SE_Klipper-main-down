# Support for 1-wire based temperature sensors
#
# Copyright (C) 2020 Alan Lord <alanslists@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import time

class CUSTOM_MACRO:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.pheaters = None
        self.heater_hot = None
        self.extruder_temp=None
        self.bed_temp=None
        self.prtouch = None
        self.gcode.register_command("CX_PRINT_LEVELING_CALIBRATION", self.cmd_CX_PRINT_LEVELING_CALIBRATION, desc=self.cmd_CX_PRINT_LEVELING_CALIBRATION_help)
        self.gcode.register_command("CX_CLEAN_CALIBRATION_FLAGS", self.cmd_CX_CLEAN_CALIBRATION_FLAGS, desc=self.cmd_CX_CLEAN_CALIBRATION_FLAGS_help)
        self.gcode.register_command("CX_PRINT_DRAW_ONE_LINE", self.cmd_CX_PRINT_DRAW_ONE_LINE, desc=self.cmd_CX_PRINT_DRAW_ONE_LINE_help)
        self.default_extruder_temp = config.getfloat("default_extruder_temp", default=240.0)
        self.default_bed_temp = config.getfloat("default_bed_temp", default=50.0)
        self.g28_ext_temp = config.getfloat("g28_ext_temp", default=140.0)
        self.nozzle_clear = config.getboolean('nozzle_clear', True)
        self.calibration = config.getint('calibration', default=0)
        self.temp_diff = config.getfloat('temp_diff', default=70)
        self.leveling_calibration = 0
        self.calibration_zoffset_flags = config.getint('calibration_zoffset_flags', default=0)
        pass


    def get_status(self, eventtime):
        return {
            'leveling_calibration': self.leveling_calibration,
            'default_extruder_temp': self.default_extruder_temp,
            'default_bed_temp': self.default_bed_temp,
            'g28_ext_temp': self.g28_ext_temp
        }

    # -------------------------------------------------------------------------
    # CX_PRINT_LEVELING_CALIBRATION
    # -------------------------------------------------------------------------
    # PURPOSE:
    #   This is the MAIN PRE-PRINT ROUTINE called by the Creality app before
    #   every print. It does the full calibration sequence in order:
    #     1. G28          - Home all axes
    #     2. CRTENSE_NOZZLE_CLEAR  - Nozzle edge-tap clean (see below)
    #     3. Z_OFFSET_CALIBRATION  - Compute z_offset via pressure sensor
    #     4. G28 Z        - Re-home Z with new z_offset
    #     5. BED_MESH_CALIBRATE    - Probe 49 points for mesh
    #     6. CXSAVE_CONFIG         - Save z_offset + mesh to disk
    #
    # WHEN CALLED:
    #   - By Creality Print app automatically before every print
    #   - Via PRINT_CALIBRATION macro (gcode_macro.cfg)
    #   - Parameters: EXTRUDER_TEMP, BED_TEMP, LEVELING_CALIBRATION (0 or 1)
    #   - If LEVELING_CALIBRATION=0: only homes, no calibration/mesh
    #   - If LEVELING_CALIBRATION=1: full calibration sequence runs
    #
    # IF ADDING SILICON BRUSH:
    #   Replace or supplement CRTENSE_NOZZLE_CLEAR (step 2) with your brush macro.
    #   The brush should run AFTER G28 and BEFORE Z_OFFSET_CALIBRATION.
    #   Nozzle temp at this point = self.g28_ext_temp (140°C by default).
    # -------------------------------------------------------------------------
    cmd_CX_PRINT_LEVELING_CALIBRATION_help = "Start Print function,three parameter:EXTRUDER_TEMP(180-300),BED_TEMP(30-100),CALIBRATION(0 or 1)"
    def cmd_CX_PRINT_LEVELING_CALIBRATION(self, gcmd):
        self.extruder_temp = gcmd.get_float('EXTRUDER_TEMP', default=self.default_extruder_temp, minval=180.0, maxval=320.0)
        if self.extruder_temp < 220.0:
            self.extruder_temp = 220.0
        self.g28_ext_temp = self.extruder_temp - self.temp_diff
        if self.g28_ext_temp > 200.0:
            self.g28_ext_temp = 200.0
        try:
            self.prtouch = self.printer.lookup_object('prtouch_v2')
        except:
            self.prtouch = self.printer.lookup_object('prtouch')
            gcmd.respond_info("self.prtouch = prtouch")
        # self.prtouch.change_hot_min_temp(self.g28_ext_temp)
        self.bed_temp = gcmd.get_float('BED_TEMP', default=self.default_bed_temp, minval=30.0, maxval=130.0)
        self.leveling_calibration = gcmd.get_int('LEVELING_CALIBRATION', default=1, minval=0, maxval=1)
        
        self.gcode.run_script_from_command('G28')
        if (self.calibration_zoffset_flags == 0):
            self.gcode.run_script_from_command('M104 S%d' % (self.g28_ext_temp))
            self.gcode.run_script_from_command('M140 S%d' % (self.bed_temp))
            # ----------------------------------------------------------------
            # CRTENSE_NOZZLE_CLEAR — Nozzle edge-tap cleaning
            # ----------------------------------------------------------------
            # PURPOSE: Cleans dried filament off the nozzle tip by tapping
            #   the bed edge/frame area at X=-5 (clr_noz_start_x in z_compensate
            #   config). Uses PHYSICAL CONTACT, NOT extrusion. Registered and
            #   handled by z_compensate_wrapper.so (closed binary).
            #
            # HOW IT WORKS:
            #   1. Heats nozzle to HOT_START_TEMP (g28_ext_temp, ~140°C)
            #   2. Moves to clr_noz_start_x=-5, clr_noz_start_y=10
            #   3. Taps the bed edge pr_clear_probe_cnt=5 times
            #   4. Heats to HOT_RUB_TEMP (extruder_temp-20, ~200°C) for rubbing
            #   5. Does NOT extrude filament
            #
            # IF ADDING SILICON BRUSH:
            #   Comment out or replace this line with your brush macro.
            #   Your brush macro should run here (after heating, before
            #   Z_OFFSET_CALIBRATION). Example:
            #     self.gcode.run_script_from_command('CLEAN_NOZZLE')
            #   where CLEAN_NOZZLE is your brush macro in gcode_macro.cfg.
            #
            # NOTE: CR-10 SE has no physical brush. CRTENSE_NOZZLE_CLEAR
            #   is the only nozzle cleaning method available by default.
            # ----------------------------------------------------------------
            self.gcode.run_script_from_command('CRTENSE_NOZZLE_CLEAR HOT_START_TEMP=%d HOT_RUB_TEMP=%d BED_ADDTEMP=%d' % (self.g28_ext_temp, self.extruder_temp - 20, self.bed_temp))
        if self.leveling_calibration == 1:
            # self.gcode.run_script_from_command('CHECK_BED_MESH AUTO_G29=1')
            if (self.calibration_zoffset_flags == 0):
                self.gcode.run_script_from_command('Z_OFFSET_CALIBRATION')
                self.gcode.run_script_from_command('M104S0')
                self.gcode.run_script_from_command('M107')
                self.gcode.run_script_from_command('G28 Z')
            else:
                self.gcode.run_script_from_command('M104S0')
                self.gcode.run_script_from_command('M107')
                self.gcode.run_script_from_command('M190 S%d' % (self.bed_temp))    
            self.gcode.run_script_from_command('BED_MESH_CALIBRATE')
            self.gcode.run_script_from_command('CXSAVE_CONFIG')

        pass

    cmd_CX_CLEAN_CALIBRATION_FLAGS_help = "Clean calibration flags"
    def cmd_CX_CLEAN_CALIBRATION_FLAGS(self, gcmd):
        self.leveling_calibration = 0
        pass

    # -------------------------------------------------------------------------
    # CX_PRINT_DRAW_ONE_LINE — Purge/Prime Line
    # -------------------------------------------------------------------------
    # PURPOSE:
    #   Draws two parallel purge lines at the LEFT EDGE of the build plate
    #   JUST BEFORE actual printing starts. This primes the nozzle with fresh
    #   filament and clears any ooze from the calibration sequence.
    #
    # WHAT IT DRAWS:
    #   Line 1: X=0.1, Y=20 → Y=180, Z=0.3mm  (extrudes 15mm filament)
    #   Line 2: X=0.4, Y=180 → Y=20,  Z=0.3mm  (extrudes 15mm more)
    #   Total: two 160mm parallel lines, 0.3mm apart, at left edge of bed.
    #   THIS IS THE LINE YOU SEE ON THE LEFT SIDE OF THE BED BEFORE PRINTING.
    #
    # WHEN CALLED:
    #   By the Creality app AFTER CX_PRINT_LEVELING_CALIBRATION completes
    #   and AFTER nozzle has reached printing temperature (extruder_temp).
    #   Waits for full temperature before drawing.
    #
    # IF ADDING SILICON BRUSH:
    #   This is a PURGE line (extrusion), NOT a nozzle cleaning step.
    #   Keep this as-is. Your silicon brush cleaning should happen in
    #   CX_PRINT_LEVELING_CALIBRATION (before calibration), NOT here.
    #   The purge line here ensures the nozzle is primed for the actual print.
    # -------------------------------------------------------------------------
    cmd_CX_PRINT_DRAW_ONE_LINE_help = "Draw one line before printing"
    def cmd_CX_PRINT_DRAW_ONE_LINE(self, gcmd):
        self.gcode.run_script_from_command('G92 E0')
        self.gcode.run_script_from_command('G1 X10 Y10 Z2 F6000')
        self.gcode.run_script_from_command('G1 Z0.2 F300')
        self.pheaters = self.printer.lookup_object('heaters')
        self.heater_hot = self.printer.lookup_object('extruder').heater
        self.gcode.respond_info("can_break_flag = %d" % (self.pheaters.can_break_flag))
        self.gcode.run_script_from_command('M104 S%d' % (self.extruder_temp))
        self.gcode.run_script_from_command('M140 S%d' % (self.bed_temp))
        self.pheaters.set_temperature(self.heater_hot, self.extruder_temp, True)
        self.gcode.respond_info("can_break_flag = %d" % (self.pheaters.can_break_flag))
        # can_break_flag values (defined in heaters.py):
        #   0 = idle / reset state
        #   1 = heating in progress (wait loop active)
        #   2 = heating interrupted / cancelled (e.g. user pause)
        #   3 = heating complete, ready to proceed with printing
        # This loop waits until heating finishes (flag leaves state 1).
        while self.pheaters.can_break_flag == 1:
            time.sleep(1)
        self.gcode.respond_info("can_break_flag = %d" % (self.pheaters.can_break_flag))
        if self.pheaters.can_break_flag == 3:
            # State 3 = heating completed successfully → draw purge line
            self.pheaters.can_break_flag = 0
            self.gcode.respond_info("can_break_flag is 3")
            self.gcode.run_script_from_command('G21')
            self.gcode.run_script_from_command('G92 E0')
            self.gcode.run_script_from_command('G1 F2400 E-0.5')
            self.gcode.run_script_from_command('SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=5')
            self.gcode.run_script_from_command('M204 S12000')
            self.gcode.run_script_from_command('G21')
            self.gcode.run_script_from_command('SET_VELOCITY_LIMIT ACCEL_TO_DECEL=6000')
            self.gcode.run_script_from_command('SET_PRESSURE_ADVANCE ADVANCE=0.04')
            self.gcode.run_script_from_command('SET_PRESSURE_ADVANCE SMOOTH_TIME=0.04')
            self.gcode.run_script_from_command('M220 S100')
            self.gcode.run_script_from_command('M221 S100')
            self.gcode.run_script_from_command('G1 Z2.0 F1200')
            self.gcode.run_script_from_command('G1 X0.1 Y20 Z0.3 F6000.0')
            self.gcode.run_script_from_command('G1 X0.1 Y180.0 Z0.3 F3000.0 E15')
            self.gcode.run_script_from_command('G1 X0.4 Y180.0 Z0.3 F3000.0')
            self.gcode.run_script_from_command('G1 X0.4 Y20 Z0.3 F3000.0 E30')
            self.gcode.run_script_from_command('G92 E0')
            self.gcode.run_script_from_command('G1 Z2.0 F1200')
            self.gcode.run_script_from_command('G1 F12000')
            self.gcode.run_script_from_command('G21')
        pass

    

def load_config(config):
    return CUSTOM_MACRO(config)
