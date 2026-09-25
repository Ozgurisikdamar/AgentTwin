package graph

import (
	"context"
	"slices"
	"strings"
)

// View is a bounded subgraph for display (spec §113): never the whole graph.
type View struct {
	Focus     []Node `json:"focus"`
	Nodes     []Node `json:"nodes"`
	Edges     []Edge `json:"edges"`
	Depth     int    `json:"depth"`
	Truncated bool   `json:"truncated"`
}

// View limits.
const (
	DefaultViewDepth = 2
	MaxViewDepth     = 4
	DefaultViewLimit = 150
	MaxViewLimit     = 500
)

// Neighborhood returns the components within depth hops of the focus, in
// either direction, at most limit of them (closest first, then by kind and
// key), and the edges between them. With kinds set, other components are
// neither shown nor walked through.
func Neighborhood(ctx context.Context, r Reader, focus []Node, depth, limit int, kinds []Kind) (View, error) {
	if depth <= 0 {
		depth = DefaultViewDepth
	}
	depth = min(depth, MaxViewDepth)
	if limit <= 0 {
		limit = DefaultViewLimit
	}
	limit = min(limit, MaxViewLimit)
	v := View{Focus: focus, Depth: depth, Nodes: []Node{}, Edges: []Edge{}}
	if v.Focus == nil {
		v.Focus = []Node{}
	}
	shown := map[string]Node{}
	order := []string{}
	take := func(n Node) bool {
		if _, ok := shown[n.ID]; ok {
			return true
		}
		if len(shown) >= limit {
			v.Truncated = true
			return false
		}
		shown[n.ID] = n
		order = append(order, n.ID)
		return true
	}
	frontier := []string{}
	for _, n := range sortedNodes(focus) {
		if take(n) {
			frontier = append(frontier, n.ID)
		}
	}
	for d := 1; d <= depth && len(frontier) > 0 && !v.Truncated; d++ {
		out, err := r.Edges(ctx, frontier, true, EdgeTypes)
		if err != nil {
			return v, err
		}
		in, err := r.Edges(ctx, frontier, false, EdgeTypes)
		if err != nil {
			return v, err
		}
		var next []string
		for _, e := range append(out, in...) {
			for _, id := range []string{e.From, e.To} {
				if _, ok := shown[id]; !ok {
					next = append(next, id)
				}
			}
		}
		nodes, err := r.Nodes(ctx, dedupe(next))
		if err != nil {
			return v, err
		}
		var level []Node
		for _, n := range nodes {
			if len(kinds) == 0 || slices.Contains(kinds, n.Kind) {
				level = append(level, n)
			}
		}
		frontier = frontier[:0]
		for _, n := range sortedNodes(level) {
			if !take(n) {
				break
			}
			frontier = append(frontier, n.ID)
		}
	}
	for _, id := range order {
		v.Nodes = append(v.Nodes, shown[id])
	}
	ids := make([]string, 0, len(order))
	ids = append(ids, order...)
	if len(ids) > 0 {
		edges, err := r.Edges(ctx, dedupe(ids), true, EdgeTypes)
		if err != nil {
			return v, err
		}
		for _, e := range edges {
			if _, ok := shown[e.To]; ok {
				v.Edges = append(v.Edges, e)
			}
		}
		slices.SortFunc(v.Edges, func(a, b Edge) int { return strings.Compare(a.ID, b.ID) })
	}
	return v, nil
}

func sortedNodes(ns []Node) []Node {
	out := slices.Clone(ns)
	slices.SortFunc(out, func(a, b Node) int {
		if a.Kind != b.Kind {
			return kindOrder[a.Kind] - kindOrder[b.Kind]
		}
		return strings.Compare(a.Key, b.Key)
	})
	return out
}
