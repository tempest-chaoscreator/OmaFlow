import QtQuick
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// OmaFlow bar chip + popup. Monitor tab for CPU/GPU/AIO temps, Curves tab
// for the five cooling modes and the CAM-style graph. The bridge in
// Service.qml owns fan2go + liquidctl; this file is the shell.
Panel {
  id: root
  moduleName: "tempest-chaoscreator.omaflow"
  ipcTarget: "omaflow"
  manageIpc: false

  readonly property var service: (bar && bar.shell && typeof bar.shell.serviceFor === "function")
    ? bar.shell.serviceFor(root.moduleName) : null
  readonly property bool ready: service ? service.ready === true : false
  readonly property bool needsSetup: service ? service.needsSetup === true : true
  readonly property string mode: service ? String(service.mode) : "silent"
  readonly property var locks: service && service.locks ? service.locks : ({})
  readonly property bool presetsLocked: service ? service.presetsLocked !== false : true
  readonly property bool gpuControl: service ? service.gpuControl === true : false
  readonly property bool aioFanControl: service ? service.aioFanControl === true : false
  readonly property bool curveLocked: service ? service.locked === true : true
  readonly property bool channelLive: Model.channelEnabled(selectedChannel, gpuControl, aioFanControl)
  readonly property var curves: service && service.curves ? service.curves : ({})
  readonly property string selectedChannel: service ? String(service.selectedChannel) : "chassis"
  readonly property var temps: service && service.temps ? service.temps : ({})
  readonly property var gpu: service && service.gpu ? service.gpu : ({})
  readonly property var aio: service && service.aio ? service.aio : ({})
  readonly property var fans: service && service.fans ? service.fans : []
  readonly property var history: service && service.history ? service.history : ({})
  readonly property var fan2go: service && service.fan2go ? service.fan2go : ({})
  readonly property var liquidctl: service && service.liquidctl ? service.liquidctl : ({})
  readonly property string lcdMode: service ? String(service.lcdMode) : "liquid"
  readonly property int lcdBrightness: service ? Number(service.lcdBrightness) : 80
  readonly property bool themeSync: service ? service.themeSync === true : true
  readonly property var tempsAxis: service && service.tempsAxis ? service.tempsAxis : Model.TEMPS
  readonly property real hottest: service ? Number(service.hottest) : 0
  readonly property string applyError: service ? String(service.applyError || "") : ""

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color dim: Qt.darker(fg, 1.4)
  readonly property color accent: Color.accent
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color tone: Model.tempTone(hottest, urgent, accent, fg)

  property string tab: "monitor"
  property string draftMode: "silent"
  property bool dropdownOpen: false

  readonly property string viewMode: tab === "curves" ? draftMode : mode
  readonly property bool viewLocked: viewMode !== "custom" && presetsLocked
  readonly property bool graphInteractive: !viewLocked && channelLive

  readonly property var presetOptions: [
    { value: "silent", label: "Silent" },
    { value: "static", label: "Static" },
    { value: "performance", label: "Performance" },
    { value: "hell", label: "Hell" }
  ]
  readonly property var channelOptions: {
    var gpuOn = root.gpuControl
    var aioOn = root.aioFanControl
    var all = [
      { value: "chassis", label: "Chassis" },
      { value: "pump", label: "Pump" },
      { value: "aio", label: "AIO" },
      { value: "gpu", label: "GPU" }
    ]
    var live = []
    var dead = []
    for (var i = 0; i < all.length; i++) {
      var id = all[i].value
      var on = (id === "gpu") ? gpuOn : (id === "aio") ? aioOn : true
      if (on) live.push(all[i])
      else dead.push(all[i])
    }
    return live.concat(dead)
  }
  readonly property var lcdOptions: [
    { value: "liquid", label: "Liquid temp" },
    { value: "accent", label: "Theme accent" },
    { value: "off", label: "Off" }
  ]

  readonly property var activePoints: {
    var rev = service ? service.curveRev : 0
    var all = curves[viewMode]
    if (!all) return []
    return Model.copyPoints(all[selectedChannel] || [])
  }
  readonly property real channelTemp: {
    if (selectedChannel === "gpu") return Number(temps.gpu)
    if (selectedChannel === "aio" || selectedChannel === "pump") return Number(temps.coolant)
    return Number(temps.cpu)
  }
  readonly property int channelMin: Model.channelMin(selectedChannel)
  readonly property var chassisFan: Model.firstFan(fans, "chassis")
  readonly property var aioFan: Model.firstFan(fans, "aio")
  readonly property var pumpFan: Model.firstFan(fans, "pump")

  readonly property string heroStatus: {
    if (!service) return "Service not loaded"
    if (!service.bridgeReady) return "Starting bridge…"
    if (needsSetup) return "Needs setup"
    var bits = []
    bits.push(Model.modeLabel(mode))
    if (temps.cpu !== undefined && temps.cpu !== null) bits.push("CPU " + Model.formatTemp(temps.cpu))
    if (temps.gpu !== undefined && temps.gpu !== null) bits.push("GPU " + Model.formatTemp(temps.gpu))
    return bits.join(" · ")
  }

  readonly property string tooltip: "OmaFlow · " + Model.modeLabel(mode) + " · " + Model.formatTemp(hottest)
  readonly property string setupPath: Qt.resolvedUrl("setup").toString().replace(/^file:\/\//, "")

  function persistSettings(values) {
    var entry = { id: root.moduleName }
    for (var existing in root.settings)
      if (existing !== "id") entry[existing] = root.settings[existing]
    for (var key in values) entry[key] = values[key]
    root.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  function pushSettings() {
    if (service && "settings" in service) service.settings = root.settings
  }

  onServiceChanged: pushSettings()
  onSettingsChanged: pushSettings()
  Component.onCompleted: pushSettings()

  function installStack() {
    if (bar && typeof bar.run === "function")
      bar.run("omarchy-launch-floating-terminal-with-presentation \"bash '" + setupPath + "'\"")
  }

  function setMode(id) {
    if (service) service.setMode(id)
    draftMode = String(id)
  }

  function pickMode(id) {
    if (tab === "curves") {
      draftMode = String(id)
      return
    }
    setMode(id)
  }

  onOpenedChanged: {
    if (opened) {
      tab = "monitor"
      draftMode = mode
    }
  }

  function toggleLock() {
    if (!service) return
    service.setPresetsLocked(!root.presetsLocked)
  }

  function toggleGpuControl() {
    if (!service) return
    var on = !root.gpuControl
    service.setGpuControl(on)
    persistSettings({ gpuControl: on })
  }

  function toggleAioFanControl() {
    if (!service) return
    var on = !root.aioFanControl
    service.setAioFanControl(on)
    persistSettings({ aioFanControl: on })
  }

  function channelIsLive(id) {
    return Model.channelEnabled(id, root.gpuControl, root.aioFanControl)
  }

  function setTab(id) {
    tab = String(id)
    if (tab === "curves") draftMode = mode
  }

  function setChannel(id) {
    if (service) service.setChannel(id)
  }

  function onPointChanged(index, value) {
    if (service) service.setPoint(index, value, viewMode)
  }

  function onDragFinished() {
    if (service) service.applyCurves()
  }

  function setLcd(id) {
    if (service) service.setLcd(id)
    persistSettings({ lcdMode: id })
  }

  function setThemeSync(on) {
    if (service) service.setThemeSync(on)
    persistSettings({ themeSync: on === true })
  }

  IpcHandler {
    target: "omaflow"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function silent(): void { root.setMode("silent") }
    function staticMode(): void { root.setMode("static") }
    function performance(): void { root.setMode("performance") }
    function hell(): void { root.setMode("hell") }
    function custom(): void { root.setMode("custom") }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰈐 " + (root.ready ? Model.formatTemp(root.hottest) : "…")
    tooltipText: root.tooltip
    foreground: root.tone
    onPressed: function(b) {
      if (b === Qt.RightButton) root.setMode(root.mode === "silent" ? "performance" : "silent")
      else if (b === Qt.MiddleButton) root.setTab(root.tab === "monitor" ? "curves" : "monitor")
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(580))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.dropdownOpen || lcdDropdown.popupOpen
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "1") root.pickMode("silent")
        else if (t === "2") root.pickMode("static")
        else if (t === "3") root.pickMode("performance")
        else if (t === "4") root.pickMode("hell")
        else if (t === "5") root.pickMode("custom")
        else if (t === "m" || t === "M") root.setTab("monitor")
        else if (t === "c" || t === "C") root.setTab("curves")
        else if (t === "r" || t === "R") { if (root.service) root.service.refresh() }
      }

      Flickable {
        id: scroll
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: column
          width: scroll.width
          spacing: Style.space(12)

          Item {
            width: parent.width
            implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight)

            Text {
              id: heroIcon
              text: "󰈐"
              color: root.tone
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
            }

            Column {
              id: heroLabels
              anchors.left: heroIcon.right
              anchors.leftMargin: Style.space(14)
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Row {
                spacing: Style.space(8)
                width: parent.width

                Text {
                  text: "OmaFlow"
                  color: root.fg
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.title
                  font.bold: true
                  anchors.verticalCenter: parent.verticalCenter
                }

                BorderSurface {
                  implicitWidth: flavorText.implicitWidth + Style.space(10)
                  implicitHeight: flavorText.implicitHeight + Style.space(4)
                  color: "transparent"
                  borderSpec: Border.controlSpec("normal", root.fg, root.accent)
                  radius: Style.cornerRadius
                  anchors.verticalCenter: parent.verticalCenter

                  Text {
                    id: flavorText
                    anchors.centerIn: parent
                    text: Model.flavorText(root.viewMode, root.hottest)
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    font.bold: true
                  }
                }
              }

              Text {
                width: parent.width
                text: root.heroStatus.toUpperCase()
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1.2
                elide: Text.ElideRight
              }
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(8)

            Button {
              width: (parent.width - parent.spacing) / 2
              text: "Telemetry"
              selected: root.tab === "monitor"
              bordered: true
              foreground: root.fg
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.body
              horizontalPadding: Style.space(18)
              verticalPadding: Style.space(8)
              onClicked: root.setTab("monitor")
            }

            Button {
              width: (parent.width - parent.spacing) / 2
              text: "Curves"
              selected: root.tab === "curves"
              bordered: true
              foreground: root.fg
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.body
              horizontalPadding: Style.space(18)
              verticalPadding: Style.space(8)
              onClicked: root.setTab("curves")
            }
          }

          Row {
            id: modeRow
            width: parent.width
            spacing: Style.space(6)

            readonly property int slotWidth: Math.max(Style.space(72), Math.floor((modeRow.width - presetLockBtn.implicitWidth - 1 - modeRow.spacing * 6) / 5))

            Repeater {
              model: root.presetOptions
              Button {
                required property var modelData
                width: modeRow.slotWidth
                text: modelData.label
                selected: root.viewMode === modelData.value
                bordered: true
                foreground: root.fg
                accent: modelData.value === "hell" ? root.urgent : root.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                horizontalPadding: Style.space(4)
                tooltipText: Model.modeLabel(modelData.value)
                onClicked: root.pickMode(modelData.value)
              }
            }

            PanelActionButton {
              id: presetLockBtn
              anchors.verticalCenter: parent.verticalCenter
              iconText: root.presetsLocked ? "󰌾" : "\u{f139b}"
              foreground: root.fg
              tooltipText: root.presetsLocked
                ? "Unlock Silent, Static, Performance, and Hell so you can edit those curves"
                : "Lock the four preset curves"
              onClicked: root.toggleLock()
            }

            Rectangle {
              width: 1
              height: Style.space(22)
              anchors.verticalCenter: parent.verticalCenter
              color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.22)
            }

            Button {
              width: modeRow.slotWidth
              text: "Custom"
              selected: root.viewMode === "custom"
              bordered: true
              foreground: root.fg
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              horizontalPadding: Style.space(4)
              tooltipText: "Always unlocked"
              onClicked: root.pickMode("custom")
            }
          }

          PanelSeparator { foreground: root.fg }

          // ---------- Setup ----------
          Column {
            visible: root.needsSetup
            width: parent.width
            spacing: Style.space(10)

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: "OmaFlow talks to fan2go for chassis fans and to liquidctl for the AIO pump and LCD. GPU fans and AIO radiator fans stay on their own control until you unlock them. Run setup once so the helper can apply curves (sudo in a terminal)."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            Button {
              text: "Install dependencies"
              iconText: "󰇚"
              bordered: true
              foreground: root.fg
              fontFamily: root.fontFamily
              tooltipText: "Opens a terminal and runs the plugin setup script (liquidctl, fan2go, polkit helper)"
              onClicked: root.installStack()
            }
          }

          // ---------- Monitor ----------
          Column {
            visible: root.tab === "monitor"
            width: parent.width
            spacing: Style.space(12)

            Row {
              width: parent.width
              spacing: Style.space(10)

              // CPU card
              BorderSurface {
                width: (parent.width - parent.spacing) / 2
                implicitHeight: cpuCol.implicitHeight + Style.space(20)
                color: Style.hoverFillFor(root.fg, root.accent)
                borderSpec: Border.controlSpec("normal", root.fg, root.accent)
                radius: Style.cornerRadius

                Column {
                  id: cpuCol
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  anchors.leftMargin: Style.space(12)
                  anchors.rightMargin: Style.space(12)
                  spacing: Style.space(4)

                  Text {
                    text: "CPU"
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                  }
                  Text {
                    text: Model.formatTemp(root.temps.cpu)
                    color: Model.tempTone(root.temps.cpu, root.urgent, root.accent, root.fg)
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.displayLarge
                    font.bold: true
                  }
                  Text {
                    text: "CCD1 " + Model.formatTemp(root.temps.cpuCcd1) + "  ·  CCD2 " + Model.formatTemp(root.temps.cpuCcd2)
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }

              // GPU card
              BorderSurface {
                width: (parent.width - parent.spacing) / 2
                implicitHeight: gpuCol.implicitHeight + Style.space(20)
                color: Style.hoverFillFor(root.fg, root.accent)
                borderSpec: Border.controlSpec("normal", root.fg, root.accent)
                radius: Style.cornerRadius

                Column {
                  id: gpuCol
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  anchors.leftMargin: Style.space(12)
                  anchors.rightMargin: Style.space(12)
                  spacing: Style.space(4)

                  Text {
                    text: Model.gpuName(root.gpu)
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                    elide: Text.ElideRight
                    width: parent.width
                  }
                  Text {
                    text: Model.formatTemp(root.temps.gpu)
                    color: Model.tempTone(root.temps.gpu, root.urgent, root.accent, root.fg)
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.displayLarge
                    font.bold: true
                  }
                  Text {
                    text: Model.formatDuty(root.gpu.util) + "  ·  " + Model.formatWatts(root.gpu.power) + "  ·  fan " + Model.formatDuty(root.gpu.fan)
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    width: parent.width
                    elide: Text.ElideRight
                  }
                }
              }
            }

            Row {
              width: parent.width
              spacing: Style.space(10)

              Repeater {
                model: [
                  { label: "Coolant", value: Model.formatTemp(root.temps.coolant) },
                  { label: "Pump", value: root.pumpFan ? Model.formatRpm(root.pumpFan.rpm) : Model.formatDuty(root.aio.pump_duty) },
                  { label: "AIO fan", value: root.aioFan ? Model.formatRpm(root.aioFan.rpm) : "—" },
                  { label: "Chassis", value: root.chassisFan ? Model.formatRpm(root.chassisFan.rpm) : "—" }
                ]
                Column {
                  required property var modelData
                  width: (parent.width - parent.spacing * 3) / 4
                  spacing: Style.space(2)
                  Text {
                    text: modelData.label
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                  }
                  Text {
                    text: String(modelData.value)
                    color: root.fg
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.subtitle
                  }
                }
              }
            }

            PanelSectionHeader {
              text: "Last minute"
              foreground: root.fg
              fontFamily: root.fontFamily
            }

            Sparkline {
              width: parent.width
              height: Style.space(72)
              values: root.history.cpu || []
              stroke: root.accent
              foreground: root.fg
              minY: 20
              maxY: 100
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              visible: root.applyError !== ""
              text: root.applyError
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          // ---------- Curves ----------
          Column {
            visible: root.tab === "curves"
            width: parent.width
            spacing: Style.space(10)

            Text {
              width: parent.width
              wrapMode: Text.Wrap
              text: "This page only edits stored curves"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Item {
              width: parent.width
              implicitHeight: Math.max(channelFlow.implicitHeight, resetBtn.implicitHeight)

              Flow {
                id: channelFlow
                anchors.left: parent.left
                anchors.right: resetBtn.left
                anchors.rightMargin: Style.space(8)
                spacing: Style.space(6)

                Repeater {
                  model: root.channelOptions
                  Row {
                    required property var modelData
                    spacing: Style.space(2)
                    opacity: root.channelIsLive(modelData.value) ? 1 : 0.45

                    Button {
                      text: modelData.label
                      selected: root.selectedChannel === modelData.value
                      bordered: true
                      foreground: root.fg
                      accent: root.accent
                      fontFamily: root.fontFamily
                      fontSize: Style.font.caption
                      tooltipText: !root.channelIsLive(modelData.value)
                        ? (modelData.value === "gpu"
                          ? "GPU fans stay on NVIDIA's curve until you unlock this"
                          : "AIO radiator fans stay unmanaged until you unlock this")
                        : Model.channelLabel(modelData.value)
                      onClicked: root.setChannel(modelData.value)
                    }

                    PanelActionButton {
                      visible: modelData.value === "gpu" || modelData.value === "aio"
                      anchors.verticalCenter: parent.verticalCenter
                      iconText: root.channelIsLive(modelData.value) ? "\u{f1a25}" : "\u{f1a26}"
                      foreground: root.fg
                      tooltipText: modelData.value === "gpu"
                        ? (root.gpuControl ? "Return GPU fans to NVIDIA" : "Let OmaFlow drive the GPU fans")
                        : (root.aioFanControl ? "Stop driving AIO radiator fans" : "Drive fans plugged into the AIO")
                      onClicked: {
                        if (modelData.value === "gpu") root.toggleGpuControl()
                        else root.toggleAioFanControl()
                      }
                    }
                  }
                }
              }

              Button {
                id: resetBtn
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: "Reset"
                bordered: true
                enabled: root.graphInteractive
                foreground: root.fg
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                tooltipText: !root.channelLive
                  ? "Unlock this channel to edit its curve"
                  : (root.viewLocked ? "Unlock the presets, or switch to Custom" : "Restore the factory curve for " + Model.channelLabel(root.selectedChannel) + " only")
                onClicked: if (root.service) root.service.resetCurve(root.viewMode)
              }
            }

            Text {
              width: parent.width
              visible: root.viewLocked && root.channelLive
              wrapMode: Text.WordWrap
              text: "The presets are locked, unlock the padlock or switch to Custom to edit curve"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Item {
              width: parent.width
              height: Style.space(220)

              CurveGraph {
                anchors.fill: parent
                opacity: root.channelLive ? 1 : 0.28
                points: root.activePoints
                temps: root.tempsAxis
                currentTemp: root.channelTemp
                interactive: root.graphInteractive
                minDuty: root.channelMin
                foreground: root.fg
                accent: root.accent
                fontFamily: root.fontFamily
                onPointChanged: function(index, value) { root.onPointChanged(index, value) }
                onDragFinished: root.onDragFinished()
              }

              Column {
                visible: !root.channelLive
                anchors.centerIn: parent
                spacing: Style.space(10)

                Text {
                  anchors.horizontalCenter: parent.horizontalCenter
                  text: root.selectedChannel === "gpu" ? "controlled by the GPU" : "not active"
                  color: root.fg
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.title
                  font.bold: true
                }

                Button {
                  anchors.horizontalCenter: parent.horizontalCenter
                  visible: root.selectedChannel === "gpu" || root.selectedChannel === "aio"
                  text: "Enable"
                  iconText: "\u{f1a26}"
                  bordered: true
                  foreground: root.fg
                  fontFamily: root.fontFamily
                  onClicked: {
                    if (root.selectedChannel === "gpu") root.toggleGpuControl()
                    else root.toggleAioFanControl()
                  }
                }
              }
            }

            Text {
              width: parent.width
              text: Model.channelLabel(root.selectedChannel) + "  ·  " + Model.modeLabel(root.viewMode)
                    + (isFinite(root.channelTemp) ? "  ·  now " + Model.formatTemp(root.channelTemp) : "")
                    + (root.selectedChannel === "pump" ? "  ·  floor 50%" : "")
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            PanelSeparator { foreground: root.fg }

            PanelSectionHeader {
              text: "AIO pump display"
              foreground: root.fg
              fontFamily: root.fontFamily
            }

            Row {
              width: parent.width
              spacing: Style.space(10)

              Dropdown {
                id: lcdDropdown
                width: Math.round(parent.width * 0.55)
                label: "Screen"
                value: root.lcdMode
                options: root.lcdOptions
                foreground: root.fg
                fontFamily: root.fontFamily
                onChanged: function(v) { root.setLcd(v) }
              }
            }

            Toggle {
              width: parent.width
              label: "Sync with theme accent"
              description: "Tint the LCD (including Liquid temp) and AIO LEDs with the Omarchy accent. Stock Liquid temp is firmware-white; with sync on, OmaFlow redraws it in the theme color."
              checked: root.themeSync
              foreground: root.fg
              accent: root.accent
              fontFamily: root.fontFamily
              onClicked: root.setThemeSync(!root.themeSync)
            }

            Row {
              width: parent.width
              spacing: Style.space(10)
              visible: root.lcdMode !== "off"

              Text {
                text: "Brightness"
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                anchors.verticalCenter: parent.verticalCenter
              }

              PanelSlider {
                width: parent.width - parent.children[0].implicitWidth - parent.spacing
                bar: root.bar
                minimum: 0
                maximum: 100
                step: 5
                integer: true
                value: root.lcdBrightness
                fillColor: root.accent
                knobColor: root.fg
                onReleased: function(v) { if (root.service) root.service.setLcdBrightness(v) }
              }
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              visible: !root.liquidctl.hasAio
              text: root.liquidctl.installed
                ? "No AIO reported by liquidctl. Chassis curves still apply."
                : "liquidctl is not installed — AIO pump, radiator, and LCD stay unmanaged until setup runs."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              visible: root.applyError !== ""
              text: root.applyError
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }
      }
    }
  }
}
