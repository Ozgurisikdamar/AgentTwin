// Package auth implements edge authentication for the control-plane: project
// API keys, development session tokens and OIDC token verification.
package auth

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"regexp"
	"strings"
)

// APIKeyPrefix marks AgentTwin API keys: atk_<8-char public id>_<secret>.
const APIKeyPrefix = "atk_"

var keyShape = regexp.MustCompile(`^atk_([a-z0-9]{8})_([A-Za-z0-9_-]{16,128})$`)

// ErrMalformedKey is returned for strings that are not AgentTwin keys.
var ErrMalformedKey = errors.New("malformed api key")

// GeneratedKey is a new key; Plaintext is shown to the user exactly once.
type GeneratedKey struct {
	Plaintext  string
	Prefix     string
	SecretHash string
}

const prefixAlphabet = "abcdefghijklmnopqrstuvwxyz0123456789"

// GenerateAPIKey creates a random key and its peppered hash.
func GenerateAPIKey(pepper string) (GeneratedKey, error) {
	var raw [8]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return GeneratedKey{}, err
	}
	var sb strings.Builder
	for _, b := range raw {
		sb.WriteByte(prefixAlphabet[int(b)%len(prefixAlphabet)])
	}
	prefix := sb.String()
	var secret [32]byte
	if _, err := rand.Read(secret[:]); err != nil {
		return GeneratedKey{}, err
	}
	secretStr := base64.RawURLEncoding.EncodeToString(secret[:])
	return GeneratedKey{
		Plaintext:  APIKeyPrefix + prefix + "_" + secretStr,
		Prefix:     prefix,
		SecretHash: HashSecret(pepper, secretStr),
	}, nil
}

// ParseAPIKey splits a key into its public prefix and secret.
func ParseAPIKey(key string) (prefix, secret string, err error) {
	m := keyShape.FindStringSubmatch(strings.TrimSpace(key))
	if m == nil {
		return "", "", ErrMalformedKey
	}
	return m[1], m[2], nil
}

// HashSecret returns HMAC-SHA256(pepper, secret) in hex. Keys are 256-bit random
// values, so a keyed hash (not a slow password hash) is appropriate; the pepper
// keeps a stolen database dump from being usable for offline verification.
func HashSecret(pepper, secret string) string {
	m := hmac.New(sha256.New, []byte(pepper))
	m.Write([]byte(secret))
	return hex.EncodeToString(m.Sum(nil))
}

// VerifySecret compares in constant time.
func VerifySecret(pepper, secret, storedHash string) bool {
	want, err := hex.DecodeString(storedHash)
	if err != nil {
		return false
	}
	m := hmac.New(sha256.New, []byte(pepper))
	m.Write([]byte(secret))
	return hmac.Equal(m.Sum(nil), want)
}

// LooksLikeAPIKey reports whether a bearer credential is an API key.
func LooksLikeAPIKey(s string) bool { return strings.HasPrefix(s, APIKeyPrefix) }
