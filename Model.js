.pragma library

var TEMPS = [20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90]
var POINT_COUNT = 15
var FAN_MIN = 20
var PUMP_MIN = 50
var CHANNELS = ["chassis", "gpu", "aio", "pump"]
var MODES = ["silent", "static", "performance", "hell", "custom"]

function clamp(v, lo, hi) {
  var n = Number(v)
  if (!isFinite(n)) n = lo
  return Math.max(lo, Math.min(hi, n))
}

function hex2(n) {
  var s = Math.round(clamp(n, 0, 255)).toString(16)
  return s.length < 2 ? "0" + s : s
}

// "#rrggbb" from a QML color, "#rrggbb", or "#aarrggbb". Qt 6 often
// stringifies as #AARRGGBB; taking the first six digits of that would
// turn Ristretto's orange into near-white (#fff38d).
function hexOf(value) {
  if (value === undefined || value === null) return ""
  if (typeof value === "string") {
    var s = value.replace(/^\s+|\s+$/g, "")
    if (s.charAt(0) === "#") s = s.slice(1)
    if (s.length === 8) s = s.slice(2)
    if (s.length === 3) s = s.charAt(0) + s.charAt(0) + s.charAt(1) + s.charAt(1) + s.charAt(2) + s.charAt(2)
    if (!/^[0-9a-fA-F]{6}$/.test(s)) return ""
    return "#" + s.toLowerCase()
  }
  if (typeof value.r === "number" && typeof value.g === "number" && typeof value.b === "number") {
    var scale = (value.r <= 1 && value.g <= 1 && value.b <= 1) ? 255 : 1
    return "#" + hex2(value.r * scale) + hex2(value.g * scale) + hex2(value.b * scale)
  }
  return hexOf(String(value))
}

function round1(v) {
  return Math.round(Number(v) * 10) / 10
}

function formatTemp(v) {
  if (v === undefined || v === null || v === "" || !isFinite(Number(v))) return "—"
  return Math.round(Number(v)) + "°"
}

function formatRpm(v) {
  if (v === undefined || v === null || !isFinite(Number(v))) return "—"
  return Math.round(Number(v)) + " rpm"
}

function formatDuty(v) {
  if (v === undefined || v === null || !isFinite(Number(v))) return "—"
  return Math.round(Number(v)) + "%"
}

function formatWatts(v) {
  if (v === undefined || v === null || !isFinite(Number(v))) return "—"
  return round1(v) + " W"
}

function tempTone(temp, urgent, accent, foreground) {
  var t = Number(temp)
  if (!isFinite(t)) return foreground
  if (t >= 85) return urgent
  if (t >= 70) return Qt.rgba(0.92, 0.62, 0.22, 1)
  return accent
}

function modeLabel(id) {
  switch (String(id)) {
  case "silent": return "Silent"
  case "static": return "Static"
  case "performance": return "Performance"
  case "hell": return "Hell"
  case "custom": return "Custom"
  default: return String(id)
  }
}

function channelLabel(id) {
  switch (String(id)) {
  case "chassis": return "Chassis"
  case "gpu": return "GPU"
  case "aio": return "AIO"
  case "pump": return "Pump"
  default: return String(id)
  }
}

function channelMin(id) {
  return String(id) === "pump" ? PUMP_MIN : 0
}

function copyPoints(points, minDuty) {
  var lo = minDuty === undefined ? 0 : Number(minDuty)
  if (!isFinite(lo)) lo = 0
  var out = []
  var src = Array.isArray(points) ? points : []
  for (var i = 0; i < POINT_COUNT; i++) {
    var v = i < src.length ? Number(src[i]) : lo
    out.push(clamp(isFinite(v) ? v : lo, lo, 100))
  }
  return out
}

// Keep the curve non-decreasing. Dragging a point up lifts every handle
// to its right; dragging down pulls every handle to its left, so you
// cannot leave a hill or a valley.
function applyMonotonic(points, index, value, minDuty) {
  var lo = minDuty === undefined ? 0 : Number(minDuty)
  if (!isFinite(lo)) lo = 0
  var pts = copyPoints(points, lo)
  var i = Math.round(Number(index))
  if (!(i >= 0 && i < POINT_COUNT)) return pts
  var v = clamp(value, lo, 100)
  pts[i] = v
  var j
  for (j = i + 1; j < POINT_COUNT; j++) {
    if (pts[j] < v) pts[j] = v
  }
  for (j = i - 1; j >= 0; j--) {
    if (pts[j] > v) pts[j] = v
  }
  for (j = 0; j < POINT_COUNT; j++) {
    if (pts[j] < lo) pts[j] = lo
  }
  return pts
}

function dutyAt(points, temp) {
  var pts = copyPoints(points)
  var t = Number(temp)
  if (!isFinite(t)) t = TEMPS[0]
  if (t <= TEMPS[0]) return pts[0]
  if (t >= TEMPS[POINT_COUNT - 1]) return pts[POINT_COUNT - 1]
  for (var i = 0; i < POINT_COUNT - 1; i++) {
    var a = TEMPS[i]
    var b = TEMPS[i + 1]
    if (t >= a && t <= b) {
      var u = (t - a) / (b - a)
      return pts[i] + (pts[i + 1] - pts[i]) * u
    }
  }
  return pts[POINT_COUNT - 1]
}

function isLocked(locks, mode) {
  if (String(mode) === "custom") return false
  if (!locks) return true
  if (typeof locks.presetsLocked === "boolean") return locks.presetsLocked !== false
  return locks[String(mode)] !== false
}

function channelEnabled(id, gpuControl, aioFanControl) {
  if (id === "gpu") return gpuControl === true
  if (id === "aio") return aioFanControl === true
  return true
}

function flavorText(mode, temp) {
  var t = Number(temp)
  if (isFinite(t) && t >= 88) return "Need a medkit"
  if (isFinite(t) && t >= 80) return "I'm on fire"
  switch (String(mode)) {
  case "silent": return "Silencer on"
  case "static": return "Camping A"
  case "performance": return "Rocket jump"
  case "hell": return "Quad damage"
  case "custom": return "sv_cheats 1"
  default: return "Frag limit"
  }
}

function hottest(temps) {
  var t = temps || {}
  var best = null
  var keys = ["cpu", "gpu", "coolant"]
  for (var i = 0; i < keys.length; i++) {
    var n = Number(t[keys[i]])
    if (isFinite(n) && (best === null || n > best)) best = n
  }
  return best
}

function fanByKind(fans, kind) {
  var list = Array.isArray(fans) ? fans : []
  var out = []
  for (var i = 0; i < list.length; i++) {
    if (list[i] && list[i].kind === kind) out.push(list[i])
  }
  return out
}

function firstFan(fans, kind) {
  var list = fanByKind(fans, kind)
  return list.length ? list[0] : null
}

function averageDuty(fans, kind) {
  var list = fanByKind(fans, kind)
  if (list.length === 0) return null
  var sum = 0
  var n = 0
  for (var i = 0; i < list.length; i++) {
    var d = Number(list[i].duty)
    if (isFinite(d)) { sum += d; n++ }
  }
  return n ? sum / n : null
}

function gpuName(gpu) {
  var name = gpu && gpu.name ? String(gpu.name) : ""
  if (name === "") return "GPU"
  return name.replace("NVIDIA GeForce ", "").replace("NVIDIA RTX ", "RTX ")
}

function lcdLabel(mode) {
  switch (String(mode)) {
  case "liquid": return "Liquid temp"
  case "accent": return "Theme accent"
  case "off": return "Off"
  default: return String(mode)
  }
}
