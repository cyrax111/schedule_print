# Snapmaker U1 probe findings

Printer firmware: 1.6.0.267_20260815150420 (Moonraker on 192.168.1.167:7125, hostname "lava")
Probed on: 2026-10-14 (idle, printing, paused, complete) and 2026-10-15 (supervised start)

Fixtures are under `tests/fixtures/real_u1/`. The printed file in Task 2 was a touchscreen print of
`Rubber Plane 2_PLA_27m9s.gcode`; the multi-colour file inspected in Task 1 was `Blade_PLA_42m57s.gcode`
(four filaments listed, only filaments 3 and 4 used).

| # | Question | Answer | Evidence |
|---|---|---|---|
| Q1 | Moonraker on 7125 reachable without API key? | Yes: `/server/info` 200, no key. | idle/server_info.json |
| Q2 | All subscribed objects present? Missing ones? | All present, none missing: webhooks, print_stats, virtual_sdcard, pause_resume, heater_bed, toolhead, extruder–extruder3, exception_manager, print_task_config, filament_detect, machine_state_manager. `print_task_config` has `filament_exist`, `filament_type`, `filament_vendor`, `filament_color_rgba` (`"RRGGBBAA"` strings) and `extruder_map_table`. | idle/objects_query.json, idle/ws_stream.jsonl |
| Q3 | print_stats.state per state; machine_state_manager values | idle `standby`; printing `printing`; paused `paused` with `pause_resume.is_paused` true; finished `complete`, which turns into `standby` with an empty filename once the finish screen is dismissed (27 s after the start-test print ended). `machine_state_manager.main_state` is 0 when idle or finished, 1 while printing or paused; `action_code` 0 throughout. `cancelled` and `error` were not observed. | idle/, printing/, paused/, complete/objects_query.json; start_test/timeline.json |
| Q4 | /server/history/list enabled? Lists touchscreen prints? | Yes. A touchscreen print is listed the moment it starts (`in_progress`, start 19:45:36Z) and as `completed` afterwards (end 20:18:36Z). Replies carry a `Date` header; the printer clock was within 1 s of the Mac's. | printing/history_list.json, complete/history_list.json |
| Q5 | Metadata fields for per-colour RGB, type, used length; tail Range supported? | Colours: `filament_colour` (`"#6F4C2F;#E72F1D;#FFFFFF;#F4C032"`). Types: `filament_type` (`"PLA;PLA;PLA;PLA"`). Used length: `filament_used_mm`, a list (`[0.0, 0.0, 2035.11, 2929.7]`), equal to the tail comment `filament used [mm]`. No `referenced_tools`. Range is honoured (206). | idle/file_metadata.json, idle/file_tail_response.json, idle/file_tail.gcode.txt |
| Q6 | Upload with print=false stores without starting? | Yes: 201 with `print_started: false`; state stayed `standby`. | start_test/upload.json, after_upload.json |
| Q7 | SET_PRINT_EXTRUDER_MAP + /printer/print/start honours the map? extruder_map_table shape? | Yes. `extruder_map_table` is a list of 32 head indices indexed by logical colour; after `CONFIG_EXTRUDER=0 MAP_EXTRUDER=2` it read `[2, 1, 2, 3, 0, …]`. The print used Head 3 (`toolhead.extruder` became `extruder2` 27.5 s after start; confirmed at the printer). | start_test/map_readback.json, timeline.json, answers.json |
| Q8 | Remote start needed touchscreen interaction? Seconds until state=printing? | No touch needed. `printing` at the first poll, 0.3 s after `/printer/print/start` returned. | start_test/answers.json, timeline.json |

## Changes needed in the scheduler core plan

- `app/printer/u1.py`: read which filaments a file uses from the metadata key `filament_used_mm` (new
  PROBE Q5 constant `META_USED_KEY`), after `referenced_tools` and before the G-code comment. Then
  `needs_gcode_tail` is false for U1 files that carry colours, types and used lengths in metadata, so no
  128 KB tail download per file.
- `app/printer/u1.py` `parse_file_colours`: a used tool at or past the end of the colour list gets an
  unknown colour instead of being dropped, so the head check covers it (closes the parked F5 residual).
- No change to `SUBSCRIBE_OBJECTS`, `IDLE_STATES`, `head_map_script` or `head_map_matches`.

## Other notes

- From the development Mac, the uv-installed Python can't reach the printer (`Errno 65`, macOS Local
  Network privacy) while curl and Apple's `/usr/bin/python3` can. The probe ran under a venv built on
  `/usr/bin/python3`. Check that the deployed container reaches the printer before the acceptance run.

## Decision

GO. The API is reachable without a key (Q1), a remote start needs no touch (Q8), and the printer honours
the head mapping set before `/printer/print/start` (Q7). History lists touchscreen prints (Q4), so a
confirmation can survive app downtime when no print happened.
