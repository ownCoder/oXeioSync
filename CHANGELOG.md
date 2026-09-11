# Changelog

What changed, and why. Entries are grouped by what they mean for someone using
oXeioSync rather than by the file they touched; the commit each one names has
the detail.

## Unreleased

Everything below is on `main` and built, but not tagged.

### No crash while the engine migrates its database

For a few seconds on start, while it migrates its database, the engine serves a
stand-in page on its API address. The REST client handed that text back as if it
were the JSON it had asked for; the transfer sampler and both event pollers each
called `.get()` on a string, and the exception hook opened an error dialog from
each of those worker threads. A widget built off the GUI thread takes the whole
process down, so the application died and left its engine running without it.

The client now treats a non-JSON answer as the engine not being ready — the
same error every caller already retries on — and an event feed that is not a
list of events likewise. The exception hook logs errors from worker threads and
keeps its dialog for the GUI thread.

### Recent images

A **Recent** tab between Dashboard and Configuration shows the images that
changed most recently in the shared folders — PNG, JPEG, GIF, WebP and BMP — as
a grid of thumbnails, newest first, whether the change was made here or pulled
in from a peer. Double-click opens a file; its menu shows it in the file manager.

It reads the engine's file-change feed (`/rest/events/disk`) on a second event
poller, which, unlike the general one, replays what the engine still remembers
on connecting, so the tab opens full rather than empty. A dropped connection to
the same engine resumes where it left off rather than replaying over what the
tab already holds, and its live polls are uncapped: the engine keeps only the
newest `limit` events, so a cap silently lost the rest of any burst. The feed is
noisy — one export run reported a thousand changes to the same hundred artboards
in a few minutes — so each file is kept once, at its latest change; deletions
remove it; everything that is not an image is dropped; and the grid redraws at
most every 400 ms however fast changes arrive.

Thumbnails decode on two worker threads, because an artboard is thousands of
pixels a side. They are cached per file, not per change: a re-exported file keeps
its last picture until the new one is ready, at most one decode per file waits
at a time, and only tiles on screen ask — a tile with no picture yet ahead of
one that is only being refreshed — so the work waiting never outgrows a
screenful however fast files change. A file reported by a folder and by another
folder shared inside it is one tile, not two taking turns redecoding it. A
changed file that will not decode keeps its old picture for two seconds, in
case the export was still being written, and then says "No preview" rather than
passing the old picture off as the new one. Quitting hides the window, drops
what has not started, and waits only for the decodes already running.

### The version counts itself

The version was typed into three files and none of them had changed since
0.1.0, so twenty-three commits later every build — and every installer made from
one — still called itself 0.1.0. An upgrade looked identical to what it
replaced.

It is no longer written anywhere. The last release tag gives major, minor and
patch, and each commit since adds one to the patch: `v0.1.0` plus 23 commits is
`0.1.23`. Every commit is a higher number with nothing to bump; a new line is
started by tagging it. The build stamps the number into the exe's version
resource, the macOS `Info.plist` and the bundle itself, and refuses to build
when there is no tag to count from rather than shipping a guess. The unused
`packaging/version_info.txt` is gone — the spec has generated its own for some
time. See [Publishing a release](README.md#publishing-a-release).

### The installer now carries the sync engine

Installing on a second machine used to fail at the first launch. No engine
ships with the application, so it offered to download one; the download asks
`api.github.com` for the release list, and GitHub rate-limits unauthenticated
callers by address. On a shared or NATed connection that is a 403, and the
dialog it produced led nowhere.

Two changes, either of which would have prevented it.

- **A build ships an engine** (`ba6069a`). `tools/fetch_engine.py` vendors one
  into `build/vendor/<os>-<arch>/`, the PyInstaller spec bundles it, and both
  the spec and the Inno Setup script now **fail the build** when it is missing
  rather than quietly producing a bundle that needs the network on first run.
  `--no-engine` opts out deliberately. A fresh machine now needs nothing: no
  Python, no Syncthing, no route to GitHub.
- **The release lookup has a second source** (`ba6069a`). When GitHub cannot be
  read, Syncthing's own metadata server is asked instead — the same list,
  without the rate limit. When both fail, the message names each attempt and
  points at the manual engine path instead of reporting one URL and stopping.

The vendoring goes through the application's own verified-download path, so the
digest is checked the same way. The offline forms (`--from-archive`,
`--from-binary`) refuse a file with no digest to check it against unless
`--allow-unverified` is passed.

Shipping someone else's binary also means shipping its terms: each build now
carries the engine's `LICENSE-engine.txt` and `AUTHORS-engine.txt` verbatim from
its release archive, plus a generated `ENGINE-NOTICE.txt` naming the exact
version, its SHA-256, and where to obtain the corresponding source. The
installer lifts all three to the top of the program folder.

The bundled copy is launched with `STNOUPGRADE=1`. It lives in the folder the
installer replaces wholesale on every update, so an engine that upgraded itself
there would be reverted by the next install and would stop matching the digest
published beside it. The downloaded copy is untouched and still updates itself.

Bundling gave the installer a new way to fail — a surviving `syncthing.exe`
holds `{app}\_internal\syncthing.exe` open, which makes `[InstallDelete]` fail
silently — so the stop-before-replace path covers the engine too, still matched
by exact path so an unrelated copy is left alone.

### Dark, and actually dark

The application is dark and no longer asks the host (`894aab7`). It used to
follow the Windows app-mode setting, which meant two things went wrong.

On Windows 10 the setting reached nothing: Qt reports the scheme correctly, but
the native style hands back a `#f0f0f0` window whatever it says, and every
colour here is read from the palette — so the dark tokens were unreachable. It
worked on Windows 11, where the platform style does follow the setting, which is
why it went unnoticed.

Following was the wrong goal anyway. The dashboard's colours are chosen for a
dark ground and validated on one. So the palette is supplied rather than asked
for, through the Fusion style, which is the only one that honours a supplied
palette throughout — under the native style the content goes dark and the tab
bar, menu and scrollbars stay light.

The offline placeholder shown when the engine cannot be reached was asking the
browser for system colours, which rendered a white panel inside a dark window at
the moment something had already gone wrong. It now uses the application's own
tokens (`06ed62f`).

### The folders card leads with what is wrong

The old card drew one row and one progress meter per folder. With twenty
folders, eighteen of them were an identical full bar captioned with the
percentage the bar had just shown, and the two that mattered — one folder with
2,310 errors, one half-synced — were drawn at the same size and weight as the
eighteen with nothing wrong (`eeef88c`).

It now opens with a count and a total, then a chip per state that is actually
present (no zeroes), then a row each for the folders that want something: the
error count and what it means, the transfer and its meter, the scan, the pause.
Everything up to date collapses into a three-column list of name and size under
a count — twenty healthy folders in four lines instead of twenty, and the size
is a fact the old row never showed.

Two things the design review measured kept that from showing on screen, and
both are fixed. The attention row's tint never painted — it was set by
stylesheet on a plain `QWidget`, which Qt ignores, so the row was byte-identical
to the card; it is now painted the way `Card` is. And the up-to-date grid gave
its spare width to the name column, which put "Aman" hundreds of pixels from its
own size and a dozen from the next folder's dot; the spare width now goes in a
gap after each size. Both are pinned by tests that render the card offscreen and
measure it, and that fail against the old code.

Memory moved up into the stat tile row. One number and its trend did not need
two fifths of the width beside the folders.

The status banner across the top of the dashboard is gone (`d5b3476`). It said
the same sentence the window title, the status bar and the tray tooltip all
already carried, and cost a card's height to do it. The engine version it showed
is in Help → About; the folder it named now has a row of its own with more
detail than the banner gave.

### macOS

A macOS build: `.app` bundle, drag-to-Applications `.dmg`, and LaunchAgent
autostart (`79fc1be`), documented in the README (`c6a5cf8`, `289a187`).

### Getting the program

The README opens with a download button that links straight at the installer
asset of the current release, rather than at the releases page five sections
down (`04405c9`). The screenshot was retaken, because the one in the README
predated the folders card, the palette and the style change — and the caption
written for it described an image that showed something else (`6674da6`).

### Tests

Grew from 207 to 267. The new ones cover the parts of this that are easy to get
quietly wrong: which release source is used and what happens when the first one
403s, that a vendored engine without a digest is refused, that the bundled
engine is looked for everywhere the packaging might put it, that the palette a
supplied dark theme installs is one the app will read as dark, and that the
folders card's ordering puts an error first even with twenty healthy folders
around it.

## Known gaps

Recorded rather than fixed, so they are not rediscovered from scratch. The
design review that found the first one is in
`.impeccable/critique/2026-07-28T05-22-51Z__oxeiosync-ui-dashboard-py.md`.

- **Three state colours fail WCAG AA on their own grounds**: the error pill at
  3.2:1, the syncing pill at 3.8:1, the up-to-date chip at 4.35:1, against a
  4.5:1 bar at that size.
- **Uptime is no longer shown anywhere.** It lived only in the removed status
  banner.
- **Only part of the rendering is tested.** The row ground and the grid's
  proximity are now measured from a real render; colour contrast and the
  chips agreeing with the rows are not.

## 0.1.0

First public release: tray supervision of the sync engine, a native dashboard,
the engine's configuration screen embedded and restyled, portable mode, and a
per-user Windows installer.
