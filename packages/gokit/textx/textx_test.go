package textx

import (
	"testing"
	"unicode/utf8"
)

func TestValidAndClean(t *testing.T) {
	for _, c := range []struct {
		in, want string
		valid    bool
	}{
		{"plain", "plain", true},
		{"çağrı · 注文 · 🙂", "çağrı · 注文 · 🙂", true},
		{"", "", true},
		{"a\x00b", "a�b", false},
		{"\x00", "�", false},
		{"bad \xff byte", "bad � byte", false},
		{"overlong \xc0\xaf", "overlong �", false},
		{"surrogate \xed\xa0\x80", "surrogate �", false},
		{"\xff\x00", "��", false},
	} {
		if got := Valid(c.in); got != c.valid {
			t.Errorf("Valid(%q) = %v", c.in, got)
		}
		got := Clean(c.in)
		if got != c.want {
			t.Errorf("Clean(%q) = %q, want %q", c.in, got, c.want)
		}
		if !Valid(got) || !utf8.ValidString(got) {
			t.Errorf("Clean(%q) is not storable: %q", c.in, got)
		}
	}
}

func TestCleanValueReachesEveryString(t *testing.T) {
	v := map[string]any{
		"ok":      "fine",
		"k\x00":   "v\x00",
		"list":    []any{"a\xff", 1.5, true, nil, map[string]any{"deep": "d\x00"}},
		"number":  3,
		"nothing": nil,
	}
	got := CleanValue(v).(map[string]any)
	if _, stale := got["k\x00"]; stale {
		t.Fatal("the uncleaned key survived")
	}
	if got["k�"] != "v�" || got["ok"] != "fine" || got["number"] != 3 {
		t.Fatalf("got %#v", got)
	}
	list := got["list"].([]any)
	if list[0] != "a�" || list[1] != 1.5 || list[2] != true || list[3] != nil {
		t.Fatalf("list %#v", list)
	}
	if list[4].(map[string]any)["deep"] != "d�" {
		t.Fatalf("nested map %#v", list[4])
	}
}

func TestCleanValueKeepsOneOfTwoKeysThatMeet(t *testing.T) {
	got := CleanValue(map[string]any{"a\x00": 1, "a�": 2}).(map[string]any)
	if len(got) != 1 {
		t.Fatalf("got %#v", got)
	}
}

func TestValueValid(t *testing.T) {
	for _, c := range []struct {
		v    any
		want bool
	}{
		{map[string]any{"a": []any{"b", 1.0, map[string]any{"c": "d"}}}, true},
		{map[string]any{"a": []any{"b", map[string]any{"c": "d\x00"}}}, false},
		{map[string]any{"a\x00": "b"}, false},
		{[]any{"ok", "\xff"}, false},
		{"\x00", false},
		{42, true},
		{nil, true},
	} {
		if got := ValueValid(c.v); got != c.want {
			t.Errorf("ValueValid(%#v) = %v", c.v, got)
		}
	}
}
