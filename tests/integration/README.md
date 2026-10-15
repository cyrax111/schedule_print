# Integration tests (u1sim)

These run the printer client, the watcher and a full scheduler run against u1sim,
a fake Klippy running Snapmaker's real Moonraker fork (github.com/zkasuran/u1-companion).
They never touch a real printer: the `sim_url` fixture refuses any host except localhost.

Needs Docker Compose >= 2.20 (`docker-compose.test.yml` uses `include:`).

    scripts/sim-up.sh
    pytest -m integration -v
    docker compose -f docker-compose.test.yml down

Pinned versions: u1-companion 69de50e, U1-Moonraker 3b8357c.

`scripts/sim-up.sh` clones both into `vendor/` (git-ignored) and differs from the
upstream stack in three ways:

- It runs u1sim's `idle_loaded` scenario (override with `U1SIM_SCENARIO`): idle, with
  all four heads loaded and identified. The default, `four_color_print`, loops a print
  of its own and empties the heads between laps.
- It creates `/userdata` in the Moonraker container. The fork checks upload space
  there, and without it every upload fails with HTTP 413 "Insufficient disk space".
- It builds and runs the pair as `linux/amd64`. Moonraker's pinned
  `streaming-form-data==1.15.0` has no arm64 wheel and the image has no compiler, so
  on Apple Silicon Docker emulates amd64.

If a test fails because of how the simulator behaves rather than our code,
mark it `@pytest.mark.xfail(reason="u1sim: <what it does>")` and note it here.
No test is marked xfail at these versions.
