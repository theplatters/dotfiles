import QtQuick
import Quickshell
import Quickshell.Io

// Single shell-level system-stats source (S-005a, S-053). One instance lives in
// shell.qml and samples mem/cpu/disk; every per-bar StatModule binds to these
// values instead of running its own Process + Timer per metric per monitor.
// Formatting (the trailing "%") matches the legacy per-module commands.
//
// S-053 native sampling: mem + cpu are sampled in-process with zero forks.
// Two FileView readers (Quickshell.Io; text() is a function and reload() a
// method on the installed 0.3.1 FileView wrapper — verified against the
// shipped quickshell-io.qmltypes) read /proc/meminfo and /proc/stat.
// watchChanges stays false — procfs inotify events are unreliable — and the
// existing 5 s poll timer drives reload(); the old data stays visible until
// each load completes. /proc/stat yields a rate, so cpu% needs two
// consecutive samples and reads "..." until the second one lands.
// Disk capacity has no procfs/statvfs equivalent visible to QML, so it keeps
// a single bounded fork: one direct python3 argv (no sh -c wrapper) on its
// own 60 s timer, because used capacity moves far slower than mem/cpu
// pressure and must not fork on the 5 s cadence.
Item {
    id: root

    property int interval: 5000
    property int diskInterval: 60000

    property string memValue: "..."
    property string cpuValue: "..."
    property string diskValue: "..."

    // Previous /proc/stat sample for the cpu delta; negative until two
    // consecutive samples exist.
    property double _prevCpuTotal: -1
    property double _prevCpuIdle: -1

    // Stale-read health signal: consecutive 5 s ticks where mem/cpu are
    // still the honest-unavailable "...". Normal operation resolves in
    // at most two ticks (cpu needs its second sample), so three in a row
    // means the FileView reads are empty/unparsed (e.g. FileView over
    // zero-size /proc files — the one runtime-unverified piece) and a
    // single console.warn makes it visible in the quickshell log.
    // Rendering stays "..."; the flags reset on the next real value.
    property int _memStaleTicks: 0
    property int _cpuStaleTicks: 0
    property bool _memStaleWarned: false
    property bool _cpuStaleWarned: false

    // Called once per 5 s tick after the FileView reloads: counts ticks
    // stuck on "..." per metric and warns once at three. Pure state
    // machine (console.warn is the only side effect); rendering and all
    // other behavior are untouched.
    function noteStatsTick() {
        if (root.memValue === "...") {
            root._memStaleTicks = Number(root._memStaleTicks) + 1;
            if (root._memStaleTicks >= 3 && !root._memStaleWarned) {
                root._memStaleWarned = true;
                console.warn("SystemStats: /proc/meminfo unreadable for 3 ticks — mem stays \"...\"");
            }
        } else {
            root._memStaleTicks = 0;
            root._memStaleWarned = false;
        }
        if (root.cpuValue === "...") {
            root._cpuStaleTicks = Number(root._cpuStaleTicks) + 1;
            if (root._cpuStaleTicks >= 3 && !root._cpuStaleWarned) {
                root._cpuStaleWarned = true;
                console.warn("SystemStats: /proc/stat unreadable for 3 ticks — cpu stays \"...\"");
            }
        } else {
            root._cpuStaleTicks = 0;
            root._cpuStaleWarned = false;
        }
    }

    // Shared accessor for StatModule views.
    function valueFor(key) {
        if (key === "mem") return root.memValue;
        if (key === "cpu") return root.cpuValue;
        if (key === "disk") return root.diskValue;
        return "...";
    }

    // used = MemTotal - MemAvailable — this is what free(1) has reported
    // since procps-ng 4.x (libprocps derived_mem_used = total - available,
    // verified against the 4.0.7 source; the installed `free` prints 16%
    // where total-free-buffers-cached-sreclaimable gives 14%).
    // percent = int(used/total*100) + "%", mirroring the legacy free/awk
    // pipeline (the Mem: row's int($3/$2 * 100) with a "%" suffix).
    // Kernels before 3.14 lack MemAvailable: fall back to the classic
    // total - free - buffers - cached - sreclaimable approximation.
    // Empty/short/malformed text keeps "..." (never NaN).
    function parseMemUsedPercent(text) {
        if (!text) return "...";
        var total = -1, avail = -1, free = -1, buffers = -1, cached = -1, reclaim = -1;
        var lines = String(text).split("\n");
        for (var i = 0; i < lines.length; ++i) {
            var m = lines[i].match(/^(\w+):\s+(\d+)/);
            if (!m) continue;
            var v = parseInt(m[2], 10);
            if (m[1] === "MemTotal") total = v;
            else if (m[1] === "MemAvailable") avail = v;
            else if (m[1] === "MemFree") free = v;
            else if (m[1] === "Buffers") buffers = v;
            else if (m[1] === "Cached") cached = v;
            else if (m[1] === "SReclaimable") reclaim = v;
        }
        if (total <= 0) return "...";
        var used = -1;
        if (avail > 0) {
            used = total - avail;
        } else if (free >= 0 && buffers >= 0 && cached >= 0 && reclaim >= 0) {
            used = total - free - buffers - cached - reclaim;
        }
        if (used < 0) return "...";
        return Math.floor(used / total * 100) + "%";
    }

    // Accumulates consecutive /proc/stat aggregate "cpu" samples.
    // total = sum of ALL cpu fields, idle = the idle field only (iowait
    // counts as busy — matches the old top pipeline, which subtracts top's
    // "id" column only).
    // percent = int(100 * (dTotal - dIdle) / dTotal) + "%".
    // Until a second sample exists (or on parse failure/empty text) returns
    // "..."; counter resets also yield "..." and recover on the next tick.
    function parseCpuPercent(text) {
        if (!text) return "...";
        var m = String(text).match(/^cpu\s+((?:\d+\s*)+)/m);
        if (!m) return "...";
        var fields = m[1].trim().split(/\s+/);
        if (fields.length < 4) return "...";
        var total = 0;
        for (var i = 0; i < fields.length; ++i) {
            var v = parseInt(fields[i], 10);
            if (isNaN(v)) return "...";
            total += v;
        }
        var idle = parseInt(fields[3], 10);
        var prevTotal = root._prevCpuTotal;
        var prevIdle = root._prevCpuIdle;
        root._prevCpuTotal = total;
        root._prevCpuIdle = idle;
        if (prevTotal < 0 || total <= prevTotal) return "...";
        var busy = (total - prevTotal) - (idle - prevIdle);
        if (busy < 0) return "...";
        return Math.floor(busy / (total - prevTotal) * 100) + "%";
    }

    FileView {
        id: memFile
        path: "/proc/meminfo"
        watchChanges: false
        blockLoading: true
        onTextChanged: root.memValue = root.parseMemUsedPercent(text())
    }

    FileView {
        id: cpuFile
        path: "/proc/stat"
        watchChanges: false
        blockLoading: true
        onTextChanged: root.cpuValue = root.parseCpuPercent(text())
    }

    // Disk has no QML-visible statvfs; one bounded direct fork (no shell).
    // Fail soft: empty output keeps the previous value, never blank/NaN.
    Process {
        id: diskProc
        command: ["python3", "-c", "import shutil; t, u, f = shutil.disk_usage('/'); print(f'{round((t-f)/t*100)}%')"]
        running: true
        stdout: StdioCollector {
            id: diskOut
        }
        onExited: (exitCode, exitStatus) => {
            var out = diskOut.text.trim();
            if (out !== "") root.diskValue = out;
        }
    }

    Timer {
        id: statsPoll
        objectName: "systemStatsPoll"
        interval: root.interval
        running: true
        repeat: true
        onTriggered: {
            memFile.reload();
            cpuFile.reload();
            root.noteStatsTick();
        }
    }

    Timer {
        id: diskPoll
        interval: root.diskInterval
        running: true
        repeat: true
        onTriggered: diskProc.running = true;
    }
}
