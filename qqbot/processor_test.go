package main

import (
	"strings"
	"testing"
	"time"
)

func TestSplitMessage(t *testing.T) {
	parts := splitMessage(strings.Repeat("你", 501), 200, 4)
	if len(parts) != 3 {
		t.Fatalf("expected 3 parts, got %d", len(parts))
	}
	for _, part := range parts {
		if len([]rune(part)) > 200 {
			t.Fatalf("part exceeds limit: %d", len([]rune(part)))
		}
	}
}

func TestSplitMessageTruncates(t *testing.T) {
	parts := splitMessage(strings.Repeat("x", 2000), 200, 2)
	if len(parts) != 2 || !strings.Contains(parts[1], "已截断") {
		t.Fatalf("unexpected truncation result: %#v", parts)
	}
}

func TestStableIDs(t *testing.T) {
	userA, conversationA := stableIDs(ScopeC2C, "user", "user")
	userB, conversationB := stableIDs(ScopeC2C, "user", "user")
	if userA != userB || conversationA != conversationB {
		t.Fatal("stable IDs changed for identical input")
	}
	if len(userA) > 128 || len(conversationA) > 128 {
		t.Fatal("stable IDs exceed app schema limits")
	}
}

func TestDeduper(t *testing.T) {
	d := NewDeduper(time.Minute)
	if !d.Accept("message") || d.Accept("message") {
		t.Fatal("deduper did not reject duplicate")
	}
}
