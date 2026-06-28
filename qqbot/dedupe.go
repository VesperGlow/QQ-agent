package main

import (
	"sync"
	"time"
)

type Deduper struct {
	mu      sync.Mutex
	entries map[string]time.Time
	ttl     time.Duration
}

func NewDeduper(ttl time.Duration) *Deduper {
	return &Deduper{entries: make(map[string]time.Time), ttl: ttl}
}

func (d *Deduper) Accept(key string) bool {
	if key == "" {
		return true
	}
	now := time.Now()
	d.mu.Lock()
	defer d.mu.Unlock()
	if expiresAt, exists := d.entries[key]; exists && now.Before(expiresAt) {
		return false
	}
	d.entries[key] = now.Add(d.ttl)
	if len(d.entries) > 2048 {
		for item, expiresAt := range d.entries {
			if now.After(expiresAt) {
				delete(d.entries, item)
			}
		}
	}
	return true
}
