package changes

import (
	"strings"
)

// DiffLine is one line of a text diff: " " kept (context), "-" removed,
// "+" added.
type DiffLine struct {
	Op   string `json:"op"`
	Text string `json:"text"`
}

// Text diff bounds: prompts are small, but their size is not trusted.
const (
	maxDiffInputLines  = 2000
	maxDiffOutputLines = 400
	diffContext        = 2
)

// DiffText is a line diff of a and b with a few lines of context around each
// change. ok is false when the texts are too long to diff (the change is
// then described by its hashes only).
func DiffText(a, b string) (lines []DiffLine, truncated, ok bool) {
	la, lb := splitLines(a), splitLines(b)
	if len(la) > maxDiffInputLines || len(lb) > maxDiffInputLines {
		return nil, false, false
	}
	ops := lcsDiff(la, lb)
	// Keep changes and their context.
	keep := make([]bool, len(ops))
	for i, op := range ops {
		if op.Op != " " {
			for j := max(0, i-diffContext); j <= min(len(ops)-1, i+diffContext); j++ {
				keep[j] = true
			}
		}
	}
	lines = []DiffLine{}
	for i, op := range ops {
		if !keep[i] {
			continue
		}
		if len(lines) == maxDiffOutputLines {
			return lines, true, true
		}
		lines = append(lines, op)
	}
	return lines, false, true
}

func splitLines(s string) []string {
	s = strings.ReplaceAll(s, "\r\n", "\n")
	if s == "" {
		return nil
	}
	return strings.Split(strings.TrimSuffix(s, "\n"), "\n")
}

// lcsDiff is the classic longest-common-subsequence diff. Inputs are
// bounded above, so the quadratic table stays small.
func lcsDiff(a, b []string) []DiffLine {
	n, m := len(a), len(b)
	t := make([][]int32, n+1)
	for i := range t {
		t[i] = make([]int32, m+1)
	}
	for i := n - 1; i >= 0; i-- {
		for j := m - 1; j >= 0; j-- {
			if a[i] == b[j] {
				t[i][j] = t[i+1][j+1] + 1
			} else {
				t[i][j] = max(t[i+1][j], t[i][j+1])
			}
		}
	}
	var out []DiffLine
	i, j := 0, 0
	for i < n && j < m {
		switch {
		case a[i] == b[j]:
			out = append(out, DiffLine{" ", a[i]})
			i++
			j++
		case t[i+1][j] >= t[i][j+1]:
			out = append(out, DiffLine{"-", a[i]})
			i++
		default:
			out = append(out, DiffLine{"+", b[j]})
			j++
		}
	}
	for ; i < n; i++ {
		out = append(out, DiffLine{"-", a[i]})
	}
	for ; j < m; j++ {
		out = append(out, DiffLine{"+", b[j]})
	}
	return out
}
