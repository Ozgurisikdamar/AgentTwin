package testutil

import (
	"context"
	"io"
	"net"
	"net/url"
	"sync"
	"testing"
)

// CutProxy forwards TCP connections to a real server and can cut them, the
// way an outage does. Cut drops every open connection and refuses new ones
// (a stopped server); Silence drops them and accepts new ones that never
// answer (a partition, where only a timeout ends the wait). Chaos tests put
// it between a client and the test RabbitMQ or PostgreSQL, so the server
// keeps running and its state can be inspected while the client is cut off
// (spec §64).
type CutProxy struct {
	target string
	ln     net.Listener

	mu       sync.Mutex
	state    proxyState
	accepted int
	conns    map[net.Conn]struct{}
	held     map[net.Conn]struct{}
	wg       sync.WaitGroup
}

type proxyState int

const (
	forwarding proxyState = iota
	refusing
	silent
)

// NewCutProxy listens on a free loopback port and forwards to target
// (host:port). It is closed on cleanup.
func NewCutProxy(t testing.TB, target string) *CutProxy {
	t.Helper()
	ln, err := new(net.ListenConfig).Listen(context.Background(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("cut proxy: listen: %v", err)
	}
	p := &CutProxy{target: target, ln: ln, conns: map[net.Conn]struct{}{}, held: map[net.Conn]struct{}{}}
	p.wg.Add(1)
	go p.accept()
	t.Cleanup(p.close)
	return p
}

// Addr is the proxy's host:port.
func (p *CutProxy) Addr() string { return p.ln.Addr().String() }

// URL returns raw with its host replaced by the proxy's address.
func (p *CutProxy) URL(t testing.TB, raw string) string {
	t.Helper()
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatalf("cut proxy: parse %q: %v", raw, err)
	}
	u.Host = p.Addr()
	return u.String()
}

// Cut drops every open connection and refuses new ones.
func (p *CutProxy) Cut() { p.set(refusing) }

// Silence drops every open connection and accepts new ones that never answer.
func (p *CutProxy) Silence() { p.set(silent) }

// Restore forwards connections again (held silent ones are closed).
func (p *CutProxy) Restore() { p.set(forwarding) }

func (p *CutProxy) set(s proxyState) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.state = s
	if s != forwarding {
		for c := range p.conns {
			_ = c.Close()
		}
	}
	if s != silent {
		for c := range p.held {
			_ = c.Close()
			delete(p.held, c)
		}
	}
}

// Open is the number of client connections currently forwarded.
func (p *CutProxy) Open() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	n := 0
	for c := range p.conns {
		if _, ok := c.(*clientSide); ok {
			n++
		}
	}
	return n
}

// Accepted is the number of connections clients have opened, answered or not.
func (p *CutProxy) Accepted() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.accepted
}

// clientSide marks the accepted end of a forwarded pair (Open counts pairs).
type clientSide struct{ net.Conn }

func (p *CutProxy) accept() {
	defer p.wg.Done()
	for {
		c, err := p.ln.Accept()
		if err != nil {
			return
		}
		p.mu.Lock()
		p.accepted++
		state := p.state
		if state == silent {
			p.held[c] = struct{}{} // never answered; closed on Restore or cleanup
		}
		p.mu.Unlock()
		switch state {
		case refusing:
			_ = c.Close() // the client sees the connection end at once
			continue
		case silent:
			continue
		}
		up, err := new(net.Dialer).DialContext(context.Background(), "tcp", p.target)
		if err != nil {
			_ = c.Close()
			continue
		}
		client := &clientSide{c}
		if !p.track(client, up) {
			continue
		}
		p.wg.Add(2)
		go p.pipe(client, up)
		go p.pipe(up, client)
	}
}

// track registers a forwarded pair, or closes it when a cut raced the dial.
func (p *CutProxy) track(client, up net.Conn) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.state != forwarding {
		_ = client.Close()
		_ = up.Close()
		return false
	}
	p.conns[client], p.conns[up] = struct{}{}, struct{}{}
	return true
}

func (p *CutProxy) pipe(dst, src net.Conn) {
	defer p.wg.Done()
	_, _ = io.Copy(dst, src)
	_ = dst.Close()
	_ = src.Close()
	p.mu.Lock()
	delete(p.conns, dst)
	delete(p.conns, src)
	p.mu.Unlock()
}

func (p *CutProxy) close() {
	_ = p.ln.Close()
	p.Cut()
	p.Restore() // closes held connections
	p.wg.Wait()
}
