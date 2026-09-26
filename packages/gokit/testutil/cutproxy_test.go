package testutil

import (
	"bufio"
	"errors"
	"net"
	"testing"
	"time"
)

// echo serves one line back per line received.
func echo(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer func() { _ = c.Close() }()
				r := bufio.NewReader(c)
				for {
					line, err := r.ReadString('\n')
					if err != nil {
						return
					}
					if _, err := c.Write([]byte(line)); err != nil {
						return
					}
				}
			}()
		}
	}()
	return ln.Addr().String()
}

func roundTrip(c net.Conn, msg string) (string, error) {
	_ = c.SetDeadline(time.Now().Add(2 * time.Second))
	if _, err := c.Write([]byte(msg + "\n")); err != nil {
		return "", err
	}
	line, err := bufio.NewReader(c).ReadString('\n')
	return line, err
}

func TestCutProxyDropsAndRefusesUntilRestored(t *testing.T) {
	p := NewCutProxy(t, echo(t))
	c, err := net.Dial("tcp", p.Addr())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close() }()
	if got, err := roundTrip(c, "before"); err != nil || got != "before\n" {
		t.Fatalf("forwarded: %q %v", got, err)
	}
	if p.Open() != 1 {
		t.Fatalf("open = %d, want 1", p.Open())
	}

	p.Cut()
	// The open connection is dropped...
	if _, err := roundTrip(c, "during"); err == nil {
		t.Fatal("a cut connection still carried data")
	}
	// ...and a new one ends before carrying anything.
	c2, err := net.Dial("tcp", p.Addr())
	if err == nil {
		if _, err := roundTrip(c2, "refused"); err == nil {
			t.Fatal("a connection made during the cut carried data")
		}
		_ = c2.Close()
	}

	// Silence: a new connection is accepted and hangs until its deadline.
	p.Silence()
	c4, err := net.Dial("tcp", p.Addr())
	if err != nil {
		t.Fatal(err)
	}
	_ = c4.SetDeadline(time.Now().Add(300 * time.Millisecond))
	start := time.Now()
	_, _ = c4.Write([]byte("hello\n"))
	if _, err := bufio.NewReader(c4).ReadString('\n'); err == nil {
		t.Fatal("a silenced connection answered")
	} else if ne := net.Error(nil); !errors.As(err, &ne) || !ne.Timeout() || time.Since(start) < 250*time.Millisecond {
		t.Fatalf("a silenced connection must hang until its deadline, got %v after %v", err, time.Since(start))
	}

	p.Restore()
	// The held connection is released on restore.
	_ = c4.SetDeadline(time.Now().Add(2 * time.Second))
	if _, err := bufio.NewReader(c4).ReadString('\n'); err == nil {
		t.Fatal("a held connection answered after restore")
	}
	_ = c4.Close()
	c3, err := net.Dial("tcp", p.Addr())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c3.Close() }()
	if got, err := roundTrip(c3, "after"); err != nil || got != "after\n" {
		t.Fatalf("restored: %q %v", got, err)
	}
	if n := p.Accepted(); n != 4 {
		t.Fatalf("accepted = %d, want 4 (forwarded, refused, silenced, restored)", n)
	}
	if u := p.URL(t, "amqp://u:p@db.example:5672/vh"); u != "amqp://u:p@"+p.Addr()+"/vh" {
		t.Fatalf("URL = %s", u)
	}
}
