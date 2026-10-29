.pragma library

// Board: array of 9, each null, "X" or "O". The human is X, the CPU is O.

var LINES = [
    [0, 1, 2], [3, 4, 5], [6, 7, 8],
    [0, 3, 6], [1, 4, 7], [2, 5, 8],
    [0, 4, 8], [2, 4, 6],
];

// null while the game is on, else {mark: "X" | "O" | null (draw), line: [cells]}.
function winner(b) {
    for (var k = 0; k < LINES.length; k++) {
        var l = LINES[k];
        if (b[l[0]] && b[l[0]] === b[l[1]] && b[l[0]] === b[l[2]])
            return { mark: b[l[0]], line: l };
    }
    return b.every(Boolean) ? { mark: null, line: [] } : null;
}

// Minimax with depth, so the CPU prefers quicker wins and slower losses.
function minimax(b, player, depth) {
    var w = winner(b);
    if (w) return w.mark === "O" ? 10 - depth : w.mark === "X" ? depth - 10 : 0;
    var best = player === "O" ? -Infinity : Infinity;
    for (var i = 0; i < 9; i++) {
        if (b[i]) continue;
        b[i] = player;
        var s = minimax(b, player === "O" ? "X" : "O", depth + 1);
        b[i] = null;
        best = player === "O" ? Math.max(best, s) : Math.min(best, s);
    }
    return best;
}

function cpuMove(board, easy) {
    var b = board.slice();
    var empty = [];
    for (var i = 0; i < 9; i++) if (!b[i]) empty.push(i);
    if (easy && Math.random() < 0.6)
        return empty[Math.floor(Math.random() * empty.length)];
    var bestScore = -Infinity, moves = [];
    for (var j = 0; j < empty.length; j++) {
        b[empty[j]] = "O";
        var s = minimax(b, "X", 1);
        b[empty[j]] = null;
        if (s > bestScore) { bestScore = s; moves = [empty[j]]; }
        else if (s === bestScore) moves.push(empty[j]);
    }
    return moves[Math.floor(Math.random() * moves.length)];
}
