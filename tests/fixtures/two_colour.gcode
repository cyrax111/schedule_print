; schedule-print integration test file
; filament used [mm] = 120.5, 30.2
G28
T0
G1 X10 Y10 F3000
T1
G1 X20 Y20 F3000
; CONFIG_BLOCK_START
; filament_colour = #FF0000;#0000FF
; filament_type = PLA;PETG
; CONFIG_BLOCK_END
