import QtQuick
import Quickshell.Io
import qs.Commons
import "Model.js" as Model

// Headless owner of the OmaFlow bridge. One per shell; every bar surface
// reads it through `bar.shell.serviceFor(id)`.
Item {
  id: root

  property var shell: null
  property var manifest: null
  property var settings: ({})

  property bool bridgeReady: false
  property bool ready: false
  property bool needsSetup: true
  property bool helperReady: false
  property string mode: "silent"
  property var locks: ({ silent: true, static: true, performance: true, hell: true, presetsLocked: true })
  property bool presetsLocked: true
  property bool gpuControl: false
  property bool aioFanControl: false
  property bool locked: true
  property var curves: ({})
  property string selectedChannel: "chassis"
  property var temps: ({})
  property var gpu: ({})
  property var aio: ({})
  property var fans: []
  property var history: ({ cpu: [], gpu: [], coolant: [] })
  property var fan2go: ({ installed: false, running: false })
  property var liquidctl: ({ installed: false, hasAio: false, name: "" })
  property string lcdMode: "liquid"
  property int lcdBrightness: 80
  property bool themeSync: true
  property var tempsAxis: [20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90]
  property real hottest: 0
  property string lastError: ""
  property string applyError: ""
  property int curveRev: 0
  property int restartDelayMs: 2000
  property bool stopping: false

  readonly property string bridgePath: Qt.resolvedUrl("scripts/omaflow_bridge.py").toString().replace(/^file:\/\//, "")
  readonly property string accentHex: Model.hexOf(Color.accent)

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function send(obj) {
    if (!bridge.running) return false
    bridge.write(JSON.stringify(obj) + "\n")
    return true
  }

  function handleLine(line) {
    var trimmed = String(line || "").trim()
    if (trimmed === "") return
    var msg
    try { msg = JSON.parse(trimmed) } catch (e) {
      console.warn("omaflow: unreadable bridge line: " + trimmed)
      return
    }
    if (msg.event === "hello") {
      bridgeReady = true
      restartDelayMs = 2000
      if (accentHex !== "") send({ op: "set_accent", hex: accentHex })
      return
    }
    if (msg.event === "state") applyState(msg)
  }

  function applyState(msg) {
    ready = msg.ready === true
    needsSetup = msg.needsSetup === true
    helperReady = msg.helperReady === true
    mode = String(msg.mode || mode)
    locks = msg.locks || locks
    presetsLocked = msg.presetsLocked !== false
    gpuControl = msg.gpuControl === true
    aioFanControl = msg.aioFanControl === true
    locked = msg.locked === true
    curves = msg.curves || curves
    selectedChannel = String(msg.selectedChannel || selectedChannel)
    temps = msg.temps || ({})
    gpu = msg.gpu || ({})
    aio = msg.aio || ({})
    fans = Array.isArray(msg.fans) ? msg.fans : []
    history = msg.history || history
    fan2go = msg.fan2go || fan2go
    liquidctl = msg.liquidctl || liquidctl
    lcdMode = String(msg.lcdMode || lcdMode)
    lcdBrightness = Number(msg.lcdBrightness) || lcdBrightness
    if ("themeSync" in msg) themeSync = msg.themeSync === true
    tempsAxis = Array.isArray(msg.tempsAxis) ? msg.tempsAxis : tempsAxis
    hottest = (msg.hottest === null || msg.hottest === undefined) ? 0 : Number(msg.hottest)
    lastError = String(msg.error || "")
    applyError = String(msg.applyError || "")
    if ("curveRev" in msg) curveRev = Number(msg.curveRev) || 0
  }

  function setMode(id) { send({ op: "set_mode", mode: String(id) }) }
  function setLock(id, lockedFlag) { send({ op: "set_presets_locked", locked: lockedFlag === true }) }
  function setPresetsLocked(lockedFlag) { send({ op: "set_presets_locked", locked: lockedFlag === true }) }
  function setGpuControl(on) { send({ op: "set_gpu_control", enabled: on === true }) }
  function setAioFanControl(on) { send({ op: "set_aio_fan_control", enabled: on === true }) }
  function setChannel(id) { send({ op: "set_channel", channel: String(id) }) }
  function setPoint(index, value, curveMode) {
    send({ op: "set_point", mode: String(curveMode || mode), channel: selectedChannel, index: index, value: value })
  }
  function applyCurves() { send({ op: "apply" }) }
  function resetCurve(curveMode) { send({ op: "reset_curve", mode: String(curveMode || mode), channel: selectedChannel }) }
  function setLcd(id) { send({ op: "set_lcd", mode: String(id), brightness: lcdBrightness }) }
  function setLcdBrightness(v) { send({ op: "set_lcd", mode: lcdMode, brightness: Math.round(v) }) }
  function setThemeSync(on) { send({ op: "set_theme_sync", enabled: on === true }) }
  function refresh() { send({ op: "refresh" }) }

  function hasSetting(name) {
    return !!(settings && settings[name] !== undefined && settings[name] !== null)
  }

  onAccentHexChanged: if (bridgeReady && accentHex !== "") send({ op: "set_accent", hex: accentHex })
  onSettingsChanged: {
    if (!bridgeReady) return
    // Only honor keys the shell actually stored. Falling back to schema
    // defaults here would wipe GPU/AIO unlocks that live in state.json.
    if (hasSetting("themeSync")) {
      var sync = settings.themeSync !== false
      if (sync !== themeSync) setThemeSync(sync)
    }
    if (hasSetting("lcdMode")) {
      var lcd = String(settings.lcdMode || "liquid")
      if (lcd !== lcdMode && (lcd === "liquid" || lcd === "accent" || lcd === "off")) setLcd(lcd)
    }
    if (hasSetting("gpuControl")) {
      var gpu = settings.gpuControl === true
      if (gpu !== gpuControl) setGpuControl(gpu)
    }
    if (hasSetting("aioFanControl")) {
      var aio = settings.aioFanControl === true
      if (aio !== aioFanControl) setAioFanControl(aio)
    }
  }

  Timer {
    id: restartTimer
    interval: root.restartDelayMs
    repeat: false
    onTriggered: bridge.running = true
  }

  Process {
    id: bridge
    command: ["python3", "-u", root.bridgePath]
    stdinEnabled: true

    stdout: SplitParser {
      splitMarker: "\n"
      onRead: function(data) { root.handleLine(data) }
    }

    stderr: SplitParser {
      splitMarker: "\n"
      onRead: function(data) { if (String(data).trim() !== "") console.warn("omaflow: " + data) }
    }

    onExited: function(exitCode, exitStatus) {
      root.bridgeReady = false
      root.ready = false
      if (root.stopping) return
      if (root.lastError === "") root.lastError = "the OmaFlow bridge exited (" + exitCode + ")"
      root.restartDelayMs = Math.min(30000, root.restartDelayMs * 2)
      restartTimer.restart()
    }
  }

  Component.onCompleted: bridge.running = true
  Component.onDestruction: {
    root.stopping = true
    restartTimer.stop()
    if (bridge.running) root.send({ op: "quit" })
  }
}
