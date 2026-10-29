import QtQuick
import net.asivery.ApploadUtils
import "logic.js" as Logic

// Tic tac toe for AppLoad. E-ink friendly: pure black and white, no animations,
// big tap targets. Everything is sized from `u` so it scales with the window.
Rectangle {
    id: root
    anchors.fill: parent
    color: "white"

    signal close

    readonly property real u: Math.min(width, height) / 100

    property string mode: "hard"        // "easy", "hard" or "pvp"
    property var board: [null, null, null, null, null, null, null, null, null]
    property string turn: "X"
    property var result: null           // Logic.winner() once the game is over
    property bool humanStarts: true     // alternates vs the CPU so it gets to open too
    property var score: ({ X: 0, O: 0, draw: 0 })

    readonly property bool vsCpu: mode !== "pvp"

    function place(i) {
        const b = board.slice();
        b[i] = turn;
        board = b;
        const w = Logic.winner(b);
        if (w) {
            result = w;
            const s = Object.assign({}, score);
            s[w.mark || "draw"]++;
            score = s;
        } else {
            turn = turn === "X" ? "O" : "X";
        }
    }

    function play(i) {
        if (result || board[i]) return;
        if (vsCpu && turn === "O") return;
        place(i);
        if (!result && vsCpu) place(Logic.cpuMove(board, mode === "easy"));
    }

    function newGame() {
        board = [null, null, null, null, null, null, null, null, null];
        result = null;
        turn = "X";
        if (vsCpu) {
            if (!humanStarts) turn = "O";
            humanStarts = !humanStarts;
            if (turn === "O") place(Logic.cpuMove(board, mode === "easy"));
        }
    }

    function setMode(m) {
        mode = m;
        score = { X: 0, O: 0, draw: 0 };
        humanStarts = true;
        newGame();
    }

    function statusText() {
        if (result) {
            if (!result.mark) return "It's a draw.";
            if (!vsCpu) return result.mark + " wins!";
            return result.mark === "X" ? "You win!" : "CPU wins.";
        }
        return vsCpu ? "Your move (X)" : turn + " to move";
    }

    Component.onCompleted: newGame()

    component Button: Rectangle {
        id: btn
        property string label
        property bool active: false
        signal clicked

        width: txt.implicitWidth + root.u * 6
        height: root.u * 8
        color: active ? "black" : "white"
        border.color: "black"
        border.width: Math.max(2, root.u * 0.5)

        Text {
            id: txt
            anchors.centerIn: parent
            text: btn.label
            color: btn.active ? "white" : "black"
            font.pixelSize: root.u * 3.6
        }
        MouseArea {
            anchors.fill: parent
            onClicked: btn.clicked()
        }
    }

    Button {
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.margins: root.u * 3
        label: "Quit ×"
        onClicked: root.close()
    }

    Column {
        anchors.centerIn: parent
        spacing: root.u * 3.5

        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "TIC TAC TOE"
            font.pixelSize: root.u * 6.5
            font.letterSpacing: root.u * 0.6
            font.family: "serif"
        }

        Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: root.u * 2
            Button { label: "vs CPU (easy)"; active: root.mode === "easy"; onClicked: root.setMode("easy") }
            Button { label: "vs CPU (hard)"; active: root.mode === "hard"; onClicked: root.setMode("hard") }
            Button { label: "2 players"; active: root.mode === "pvp"; onClicked: root.setMode("pvp") }
        }

        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: root.statusText()
            font.pixelSize: root.u * 5
        }

        Rectangle {
            id: boardFrame
            anchors.horizontalCenter: parent.horizontalCenter
            width: root.u * 62
            height: width
            color: "black"

            // Fast refresh for the board so moves show up quickly.
            DisplayMethodArea {
                anchors.fill: parent
                displayMethod: DisplayMethodArea.Fast
            }

            Grid {
                anchors.fill: parent
                anchors.margins: root.u * 0.8
                columns: 3
                spacing: root.u * 0.8

                Repeater {
                    model: 9
                    Rectangle {
                        id: cell
                        readonly property string mark: root.board[index] || ""
                        readonly property bool winning: !!root.result && root.result.line.indexOf(index) !== -1
                        readonly property color ink: winning ? "white" : "black"
                        readonly property real stroke: width * 0.11

                        width: (boardFrame.width - root.u * 3.2) / 3
                        height: width
                        color: winning ? "black" : "white"

                        // X: two crossed bars
                        Repeater {
                            model: cell.mark === "X" ? [45, -45] : []
                            Rectangle {
                                anchors.centerIn: parent
                                width: cell.width * 0.85
                                height: cell.stroke
                                radius: height / 2
                                rotation: modelData
                                color: cell.ink
                            }
                        }

                        // O: a ring
                        Rectangle {
                            visible: cell.mark === "O"
                            anchors.centerIn: parent
                            width: cell.width * 0.64
                            height: width
                            radius: width / 2
                            color: "transparent"
                            border.width: cell.stroke
                            border.color: cell.ink
                        }

                        MouseArea {
                            anchors.fill: parent
                            onClicked: root.play(index)
                        }
                    }
                }
            }
        }

        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: (root.vsCpu ? "You: " : "X: ") + root.score.X
                + "    ·    " + (root.vsCpu ? "CPU: " : "O: ") + root.score.O
                + "    ·    Draws: " + root.score.draw
            font.pixelSize: root.u * 3.8
        }

        Button {
            anchors.horizontalCenter: parent.horizontalCenter
            label: "New game"
            onClicked: root.newGame()
        }
    }
}
