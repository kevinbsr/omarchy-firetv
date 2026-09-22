import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "kevin.firetv"

  property string status: "checking"
  property string app: ""
  property string packageName: ""
  property string title: ""
  property string artist: ""
  property string album: ""
  property string artworkUri: ""
  property int actions: 0
  property string lastCheckedText: ""
  property string lastConnectedText: ""
  property string pendingAction: ""
  property string pendingMode: "--control"
  property var installedApps: []
  property var historyEntries: []
  property int latencyMs: -1
  property var discoveredHosts: []
  property string setupMessage: ""
  property string pendingHost: ""
  property string controlError: ""
  property bool popupOpen: false
  property bool hasSeenSnapshot: false
  property string lastMediaKey: ""
  readonly property bool opened: popupOpen
  readonly property bool privacyMode: Boolean(root.setting("privacyMode", false))
  readonly property bool notificationsEnabled: Boolean(root.setting("notificationsEnabled", true))
  readonly property bool historyEnabled: Boolean(root.setting("historyEnabled", false))

  readonly property string appIconSource: {
    var icons = {
      "com.amazon.firetv.youtube": "file:///usr/share/icons/Papirus/64x64/apps/youtube.svg",
      "com.google.android.youtube.tv": "file:///usr/share/icons/Papirus/64x64/apps/youtube.svg",
      "com.netflix.ninja": "file:///usr/share/icons/Papirus/64x64/apps/netflix.svg",
      "com.spotify.tv.android": "file:///usr/share/icons/hicolor/64x64/apps/spotify.png",
      "com.plexapp.android": "file:///usr/share/icons/Papirus/64x64/apps/plex-htpc.svg"
    }
    return icons[packageName] || ""
  }
  readonly property bool connected: status !== "offline" && status !== "unauthorized" &&
                                    status !== "not-configured" &&
                                    status !== "checking" && status !== "unknown"
  readonly property string displayText: {
    if (status === "playing" || status === "paused")
      return app + (title ? " · " + title : "")
    if (status === "app") return app
    if (status === "home") return "Fire TV idle"
    if (status === "screensaver") return "Fire TV screensaver"
    if (status === "unauthorized") return "Fire TV authorization needed"
    if (status === "offline") return "Fire TV offline"
    if (status === "not-configured") return "Set up Fire TV"
    return "Fire TV checking"
  }
  readonly property string tooltipText: {
    if (privacyMode) return "Fire TV · " + statusLabel + " · private"
    var prefix = status === "paused" ? "Paused on Fire TV: " : "Fire TV: "
    return prefix + displayText
  }
  readonly property string statusLabel: {
    if (status === "playing") return "Playing"
    if (status === "paused") return "Paused"
    if (status === "app") return "App open"
    if (status === "home") return "Home screen"
    if (status === "screensaver") return "Screensaver"
    if (status === "unauthorized") return "Authorization needed"
    if (status === "offline") return "Offline"
    if (status === "not-configured") return "Setup needed"
    return "Checking"
  }
  readonly property string connectionLabel: connected ? "Connected" :
    status === "unauthorized" ? "Authorization needed" : "Disconnected"

  function open() {
    popupOpen = true
    refresh()
    if (!String(root.setting("host", ""))) discoverDevices()
    else refreshApps()
  }
  function close() { popupOpen = false }

  function setPreference(key, value) {
    var entry = { id: root.moduleName }
    var base = root.settings && typeof root.settings === "object" ? root.settings : {}
    for (var name in base) if (name !== "id") entry[name] = base[name]
    entry[key] = value
    root.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
    if (key === "historyEnabled") refresh()
  }

  function pathFromUrl(url) {
    var value = String(url || "")
    return value.indexOf("file://") === 0
      ? decodeURIComponent(value.substring(7)) : value
  }

  function refresh() {
    if (!reader.running) reader.running = true
  }

  function refreshApps() {
    if (!appsProc.running) appsProc.running = true
  }

  function discoverDevices() {
    if (discoveryProc.running) return
    setupMessage = "Searching the local network…"
    discoveredHosts = []
    discoveryProc.running = true
  }

  function connectHost(host) {
    if (pairProc.running) return
    pendingHost = String(host).trim()
    setupMessage = "Connecting to " + pendingHost + "…"
    pairProc.running = true
  }

  function actionAvailable(action) {
    if (!connected || (status !== "playing" && status !== "paused")) return false
    if (action === "previous") return (actions & 16) !== 0
    if (action === "next") return (actions & 32) !== 0
    if (action === "play-pause") return (actions & (status === "playing" ? 2 : 4)) !== 0
    if (action === "rewind") return (actions & 8) !== 0
    if (action === "fast-forward") return (actions & 64) !== 0
    return false
  }

  function remoteAvailable() { return connected }

  function runControl(action) {
    if (controlProc.running || !(actionAvailable(action) ||
        (["up", "down", "left", "right", "select", "back", "home"].indexOf(action) >= 0 && remoteAvailable()))) return
    pendingAction = action
    pendingMode = "--control"
    controlError = ""
    controlProc.running = true
  }

  function runUtility(action) {
    if (controlProc.running) return
    pendingMode = "--control"
    pendingAction = action
    controlError = ""
    controlProc.running = true
  }

  function launchApp(packageName) {
    if (controlProc.running || !connected) return
    pendingMode = "--launch"
    pendingAction = packageName
    controlError = ""
    controlProc.running = true
  }

  function sendText(value) {
    if (controlProc.running || !connected || !value.trim()) return
    pendingMode = "--type"
    pendingAction = value
    controlError = ""
    controlProc.running = true
  }

  function ownsNotifications() {
    if (!bar || typeof bar.moduleWidgets !== "function") return true
    var instances = bar.moduleWidgets(moduleName)
    return instances.length > 0 && instances[0] === root
  }

  function update(raw) {
    try {
      var result = JSON.parse(raw)
      status = String(result.status || "unknown")
      app = String(result.app || "")
      packageName = String(result.package || "")
      title = String(result.title || "")
      artist = String(result.artist || "")
      album = String(result.album || "")
      artworkUri = String(result.artworkUri || "")
      actions = Number(result.actions || 0)
      latencyMs = Number(result.latencyMs === undefined ? -1 : result.latencyMs)
      historyEntries = result.history || []
      lastCheckedText = Qt.formatTime(new Date(), "HH:mm:ss")
      if (connected) lastConnectedText = lastCheckedText
      var mediaKey = (status === "playing" || status === "paused") && title
        ? packageName + "\n" + title : ""
      if (hasSeenSnapshot && mediaKey && mediaKey !== lastMediaKey &&
          notificationsEnabled && !privacyMode && ownsNotifications())
        Quickshell.execDetached(["notify-send", "-a", "Fire TV", "-i", "video-display",
                                 "Now watching on " + app, title])
      lastMediaKey = mediaKey
      hasSeenSnapshot = true
    } catch (error) {
      status = "offline"
      app = ""
      packageName = ""
      title = ""
      artist = ""
      album = ""
      artworkUri = ""
      actions = 0
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰟴"
    active: root.popupOpen
    tooltipText: root.tooltipText
    onPressed: function(b) {
      if (b === Qt.LeftButton) {
        if (root.popupOpen) root.close()
        else root.open()
      }
    }
  }

  PopupCard {
    id: popup
    anchorItem: button
    bar: root.bar
    owner: root
    open: root.popupOpen
    contentWidth: popup.fittedContentWidth(Style.space(360))
    contentHeight: popup.fittedContentHeight(details.implicitHeight)

    Column {
      id: details
      anchors.fill: parent
      spacing: Style.space(10)

      Column {
        width: parent.width
        spacing: Style.space(10)
        visible: root.status === "not-configured"

        Text {
          text: "Connect your Fire TV"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }
        Text {
          width: parent.width
          text: "On the TV: Settings → My Fire TV → Developer options → ADB Debugging: On. Keep this computer on the same Wi-Fi."
          wrapMode: Text.WordWrap
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption
        }
        Text {
          width: parent.width
          text: "If Developer options is hidden, open About and press Select seven times on your Fire TV name."
          wrapMode: Text.WordWrap
          color: root.bar ? root.bar.foreground : Color.foreground
          opacity: 0.7
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption
        }
        Text {
          text: discoveryProc.running ? "Searching…" : "󰑐 Find Fire TV"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.body
          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.discoverDevices()
          }
        }
        Repeater {
          model: root.discoveredHosts
          Rectangle {
            id: foundDevice
            readonly property string host: modelData
            width: Style.space(210)
            height: Style.space(30)
            radius: Style.space(8)
            color: root.bar ? Qt.darker(root.bar.foreground, 3.2) : "#343434"
            Text {
              anchors.centerIn: parent
              text: "TV at " + foundDevice.host + "  → Connect"
              color: root.bar ? root.bar.foreground : Color.foreground
              font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
              font.pixelSize: Style.font.caption
            }
            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: root.connectHost(foundDevice.host)
            }
          }
        }
        Text {
          width: parent.width
          text: root.setupMessage
          visible: text !== ""
          wrapMode: Text.WordWrap
          color: root.bar ? root.bar.foreground : Color.foreground
          opacity: 0.7
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption
        }
        Text {
          text: "Or enter the TV's IP address"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption
        }
        Row {
          width: parent.width
          spacing: Style.space(8)
          Rectangle {
            width: Style.space(190)
            height: Style.space(31)
            radius: Style.space(8)
            color: root.bar ? Qt.darker(root.bar.foreground, 3.2) : "#343434"
            TextInput {
              id: tvIpInput
              anchors.fill: parent
              anchors.leftMargin: Style.space(8)
              anchors.rightMargin: Style.space(8)
              verticalAlignment: TextInput.AlignVCenter
              inputMethodHints: Qt.ImhNoPredictiveText
              color: root.bar ? root.bar.foreground : Color.foreground
              font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
              font.pixelSize: Style.font.caption
              onAccepted: root.connectHost(text)
            }
            Text {
              anchors.fill: tvIpInput
              verticalAlignment: Text.AlignVCenter
              text: "192.168.x.x"
              visible: tvIpInput.text === "" && !tvIpInput.activeFocus
              color: root.bar ? root.bar.foreground : Color.foreground
              opacity: 0.45
              font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
              font.pixelSize: Style.font.caption
            }
          }
          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "Connect"
            color: root.bar ? root.bar.foreground : Color.foreground
            font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
            font.pixelSize: Style.font.caption
            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: root.connectHost(tvIpInput.text)
            }
          }
        }
        Text {
          width: parent.width
          text: "Approve “Allow USB debugging” on the TV when prompted. If ADB is missing on Omarchy, run: omarchy pkg add android-tools"
          wrapMode: Text.WordWrap
          color: root.bar ? root.bar.foreground : Color.foreground
          opacity: 0.7
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption
        }
      }

      Column {
        id: mainContent
        width: parent.width
        spacing: Style.space(10)
        visible: root.status !== "not-configured"

      Row {
        width: parent.width
        spacing: Style.space(12)

        Rectangle {
          id: logoTile
          width: Style.space(54)
          height: width
          radius: Style.space(12)
          color: root.bar ? Qt.darker(root.bar.foreground, 3.2) : "#343434"

          Image {
            id: appLogo
            anchors.centerIn: parent
            width: parent.width - Style.space(12)
            height: width
            fillMode: Image.PreserveAspectFit
            source: /^(https?:|file:)/.test(root.artworkUri) ? root.artworkUri : root.appIconSource
            visible: status === Image.Ready
          }

          Text {
            anchors.centerIn: parent
            visible: !appLogo.visible
            text: root.app ? root.app.substring(0, 2).toUpperCase() : "󰟴"
            color: root.bar ? root.bar.foreground : Color.foreground
            font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
            font.pixelSize: Style.font.subtitle
            font.bold: true
          }
        }

        Column {
          width: parent.width - logoTile.width - parent.spacing
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(4)

          Text {
            width: parent.width
            text: root.app || "Fire TV"
            color: root.bar ? root.bar.foreground : Color.foreground
            font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
            font.pixelSize: Style.font.subtitle
            font.bold: true
            elide: Text.ElideRight
          }

          Text {
            width: parent.width
            text: root.connectionLabel + " · " + root.statusLabel
            color: root.bar ? root.bar.foreground : Color.foreground
            opacity: 0.7
            font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }
      }

      Text {
        width: parent.width
        text: root.title || (root.status === "app" ? "No title reported by this app" : root.displayText)
        color: root.bar ? root.bar.foreground : Color.foreground
        opacity: root.title ? 1 : 0.65
        font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
        font.pixelSize: Style.font.body
        wrapMode: Text.WordWrap
        maximumLineCount: 4
        elide: Text.ElideRight
      }

      Text {
        width: parent.width
        visible: root.artist !== "" || root.album !== ""
        text: root.artist + (root.artist && root.album ? " · " : "") + root.album
        color: root.bar ? root.bar.foreground : Color.foreground
        opacity: 0.65
        font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }

      Row {
        width: parent.width
        spacing: Style.space(12)

        Column {
          id: remoteColumn
          width: Style.space(127)
          spacing: Style.space(5)

          Text {
            text: "REMOTE"
            color: root.bar ? root.bar.foreground : Color.foreground
            opacity: 0.6
            font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
            font.pixelSize: Style.font.caption
            font.bold: true
          }

          Grid {
            columns: 3
            spacing: Style.space(5)

            Repeater {
              model: ["previous", "play-pause", "next",
                      "rewind", "up", "fast-forward",
                      "left", "select", "right",
                      "back", "down", "home"]
              Rectangle {
                id: remoteKey
                readonly property string actionId: modelData
                readonly property bool isPlayback: actionId === "previous" || actionId === "play-pause" || actionId === "next"
                readonly property bool isSeek: actionId === "rewind" || actionId === "fast-forward"
                width: Style.space(39)
                height: width
                radius: Style.space(9)
                visible: !isPlayback || root.status === "playing" || root.status === "paused"
                color: isSeek && !enabled ? "transparent" : (root.bar ? Qt.darker(root.bar.foreground, 3.2) : "#343434")
                enabled: !controlProc.running && (isPlayback || isSeek ? root.actionAvailable(actionId) : root.remoteAvailable())

                Text {
                  anchors.centerIn: parent
                  text: remoteKey.actionId === "previous" ? "󰒮" :
                        remoteKey.actionId === "play-pause" ? (root.status === "playing" ? "󰏤" : "󰐊") :
                        remoteKey.actionId === "next" ? "󰒭" :
                        remoteKey.actionId === "rewind" ? "󰓕" :
                        remoteKey.actionId === "fast-forward" ? "󰓖" :
                        remoteKey.actionId === "up" ? "󰁝" :
                        remoteKey.actionId === "down" ? "󰁅" :
                        remoteKey.actionId === "left" ? "󰁍" :
                        remoteKey.actionId === "right" ? "󰁔" :
                        remoteKey.actionId === "back" ? "󰌍" :
                        remoteKey.actionId === "home" ? "󰋜" : "OK"
                  visible: remoteKey.enabled || !remoteKey.isSeek
                  color: root.bar ? root.bar.foreground : Color.foreground
                  opacity: remoteKey.enabled ? 1 : 0.4
                  font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
                  font.pixelSize: Style.font.subtitle
                }
                MouseArea {
                  anchors.fill: parent
                  cursorShape: remoteKey.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                  onClicked: root.runControl(remoteKey.actionId)
                }
              }
            }
          }
        }

        Column {
          width: parent.width - remoteColumn.width - parent.spacing
          spacing: Style.space(5)

          Text {
            text: "APPS"
            color: root.bar ? root.bar.foreground : Color.foreground
            opacity: 0.6
            font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
            font.pixelSize: Style.font.caption
            font.bold: true
          }

          Flow {
            id: appsGrid
            width: parent.width
            spacing: Style.space(4)
            Repeater {
              model: root.installedApps
              Rectangle {
                id: appShortcut
                readonly property string appPackage: modelData.package
                readonly property string appName: modelData.name
                readonly property bool isCurrentApp: root.connected && appPackage === root.packageName
                width: index === root.installedApps.length - 1 && root.installedApps.length % 2 === 1
                  ? appsGrid.width : Math.floor((appsGrid.width - appsGrid.spacing) / 2)
                height: Style.space(30)
                radius: Style.space(7)
                color: root.bar ? Qt.darker(root.bar.foreground, 3.2) : "#343434"
                border.width: isCurrentApp ? 2 : 0
                border.color: Color.accent
                enabled: root.connected && !controlProc.running
                opacity: enabled ? 1 : 0.4
                Text {
                  id: appLabel
                  anchors.fill: parent
                  anchors.leftMargin: Style.space(6)
                  anchors.rightMargin: Style.space(6)
                  text: appShortcut.appName
                  horizontalAlignment: Text.AlignHCenter
                  verticalAlignment: Text.AlignVCenter
                  elide: Text.ElideRight
                  color: root.bar ? root.bar.foreground : Color.foreground
                  font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
                  font.pixelSize: Style.font.caption
                }
                MouseArea {
                  anchors.fill: parent
                  cursorShape: appShortcut.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                  onClicked: root.launchApp(appShortcut.appPackage)
                }
              }
            }
          }
          Text {
            width: parent.width
            visible: root.installedApps.length === 0
            text: root.connected ? "Loading apps…" : "Apps unavailable"
            color: root.bar ? root.bar.foreground : Color.foreground
            opacity: 0.6
            font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
            font.pixelSize: Style.font.caption
          }
        }
      }

      Text {
        text: "TYPE ON TV"
        color: root.bar ? root.bar.foreground : Color.foreground
        opacity: 0.6
        font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
        font.pixelSize: Style.font.caption
        font.bold: true
      }

      Row {
        width: parent.width
        spacing: Style.space(6)
        Rectangle {
          width: parent.width - sendLabel.implicitWidth - Style.space(26)
          height: Style.space(32)
          radius: Style.space(8)
          color: root.bar ? Qt.darker(root.bar.foreground, 3.2) : "#343434"
          TextInput {
            id: tvInput
            anchors.fill: parent
            anchors.leftMargin: Style.space(8)
            anchors.rightMargin: Style.space(8)
            verticalAlignment: TextInput.AlignVCenter
            clip: true
            color: root.bar ? root.bar.foreground : Color.foreground
            font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
            font.pixelSize: Style.font.caption
            maximumLength: 100
            onAccepted: root.sendText(text)
          }
          Text {
            anchors.fill: tvInput
            verticalAlignment: Text.AlignVCenter
            text: "Focus a TV search field first"
            visible: tvInput.text === "" && !tvInput.activeFocus
            color: root.bar ? root.bar.foreground : Color.foreground
            opacity: 0.45
            font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
            font.pixelSize: Style.font.caption
          }
        }
        Text {
          id: sendLabel
          anchors.verticalCenter: parent.verticalCenter
          text: "Send ↵"
          color: root.bar ? root.bar.foreground : Color.foreground
          opacity: root.connected ? 1 : 0.4
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption
          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.sendText(tvInput.text)
          }
        }
      }

      Row {
        width: parent.width
        spacing: Style.space(14)
        Text {
          text: root.historyEnabled ? "󰋚 Timeline on" : "󰋚 Timeline off"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption
          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.setPreference("historyEnabled", !root.historyEnabled)
          }
        }
        Text {
          visible: root.historyEnabled && root.historyEntries.length > 0
          text: "Clear history"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption
          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.runUtility("clear-history")
          }
        }
      }

      Column {
        width: parent.width
        spacing: Style.space(4)
        visible: root.historyEnabled
        Repeater {
          model: root.historyEntries.slice(0, 5)
          Text {
            width: parent.width
            text: Qt.formatDateTime(new Date(modelData.at), "MMM d HH:mm") +
                  " · " + modelData.app + " · " + modelData.title
            color: root.bar ? root.bar.foreground : Color.foreground
            opacity: 0.7
            font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }
        Text {
          visible: root.historyEntries.length === 0
          text: "No titles recorded yet"
          color: root.bar ? root.bar.foreground : Color.foreground
          opacity: 0.6
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption
        }
      }

      Row {
        width: parent.width
        spacing: Style.space(14)
        Text {
          text: root.notificationsEnabled ? "󰂚 Alerts on" : "󰂛 Alerts off"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption
          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.setPreference("notificationsEnabled", !root.notificationsEnabled)
          }
        }
        Text {
          text: root.privacyMode ? "󰈉 Private on" : "󰈈 Private off"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption
          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.setPreference("privacyMode", !root.privacyMode)
          }
        }
      }

      Text {
        width: parent.width
        visible: root.controlError !== ""
        text: root.controlError
        color: "#ef7777"
        font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      Row {
        width: parent.width
        spacing: Style.space(8)

        Rectangle {
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(7)
          height: width
          radius: width / 2
          color: root.connected ? "#53c985" : "#ef7777"
        }

        Text {
          text: root.connected ? "Updated " + (root.lastCheckedText || "just now") :
            root.lastConnectedText ? "Last connected " + root.lastConnectedText : "Not connected yet"
          color: root.bar ? root.bar.foreground : Color.foreground
          opacity: 0.7
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption
        }

        Text {
          text: root.connected && root.latencyMs >= 0 ? root.latencyMs + " ms" : ""
          color: root.bar ? root.bar.foreground : Color.foreground
          opacity: 0.6
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption
        }

        Text {
          text: "↻ Refresh"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption

          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.refresh()
          }
        }

        Text {
          text: "Reconnect"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
          font.pixelSize: Style.font.caption
          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.runUtility("reconnect")
          }
        }
      }
      Text {
        text: "Change Fire TV"
        color: root.bar ? root.bar.foreground : Color.foreground
        opacity: 0.6
        font.family: root.bar ? root.bar.fontFamily : "JetBrainsMono Nerd Font"
        font.pixelSize: Style.font.caption
        MouseArea {
          anchors.fill: parent
          cursorShape: Qt.PointingHandCursor
          onClicked: {
            root.setPreference("host", "")
            root.status = "not-configured"
            root.installedApps = []
            root.discoverDevices()
          }
        }
      }
      }
    }
  }

  Process {
    id: reader
    command: ["python3", root.pathFromUrl(Qt.resolvedUrl("firetv_status.py")),
              String(root.setting("host", "")), "--history",
              root.historyEnabled ? "on" : "off"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.update(text)
    }
    onExited: function(exitCode) {
      if (exitCode !== 0) root.status = "offline"
    }
  }

  Process {
    id: appsProc
    command: ["python3", root.pathFromUrl(Qt.resolvedUrl("firetv_status.py")),
              String(root.setting("host", "")), "--apps"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var result = JSON.parse(text)
          root.installedApps = result.ok ? result.apps : []
        } catch (error) {
          root.installedApps = []
        }
      }
    }
  }

  Process {
    id: discoveryProc
    command: ["python3", root.pathFromUrl(Qt.resolvedUrl("firetv_status.py")), "--discover"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var result = JSON.parse(text)
          root.discoveredHosts = result.hosts || []
          root.setupMessage = root.discoveredHosts.length
            ? root.discoveredHosts.length + " possible Fire TV device(s) found"
            : "No ADB device found. Check ADB Debugging, or enter the TV's IP below."
          if (root.discoveredHosts.length === 1) root.connectHost(root.discoveredHosts[0])
        } catch (error) {
          root.setupMessage = "Search failed. Enter the TV's IP below."
        }
      }
    }
  }

  Process {
    id: pairProc
    command: ["python3", root.pathFromUrl(Qt.resolvedUrl("firetv_status.py")),
              "--pair", root.pendingHost]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var result = JSON.parse(text)
          if (result.ok) {
            root.setupMessage = "Connected to Fire TV " + result.model
            root.setPreference("host", result.host)
            root.status = "checking"
            root.refresh()
            root.refreshApps()
          } else root.setupMessage = String(result.error || "Could not connect")
        } catch (error) {
          root.setupMessage = "Could not connect. Try again."
        }
      }
    }
  }

  Process {
    id: controlProc
    command: ["python3", root.pathFromUrl(Qt.resolvedUrl("firetv_status.py")),
              String(root.setting("host", "")),
              root.pendingMode, root.pendingAction]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var result = JSON.parse(text)
          root.controlError = result.ok ? "" : String(result.error || "Control failed")
        } catch (error) {
          root.controlError = "Control failed"
        }
        refreshSoon.restart()
      }
    }
    onExited: function(exitCode) {
      if (exitCode !== 0) root.controlError = "Control failed"
    }
  }

  Timer {
    id: refreshSoon
    interval: 700
    repeat: false
    onTriggered: root.refresh()
  }

  Timer {
    interval: Math.max(10, Number(root.setting("refreshIntervalSec", 15))) * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }
}
