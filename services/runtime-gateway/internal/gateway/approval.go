package gateway

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"regexp"
	"time"
)

// Approval statuses. EXPIRED is also what a PENDING or APPROVED approval
// reads as once its expiry has passed.
const (
	StatusPending  = "PENDING"
	StatusApproved = "APPROVED"
	StatusDenied   = "DENIED"
	StatusExpired  = "EXPIRED"
	StatusUsed     = "USED"
)

// TokenPrefix marks approval tokens (and lets secret scanners find them).
const TokenPrefix = "apt_"

// TokenTTL is the longest a token lives; never past its approval's expiry.
const TokenTTL = 10 * time.Minute

// TokenPattern is the shape of a token: the prefix and 32 random bytes.
var TokenPattern = regexp.MustCompile(`^apt_[A-Za-z0-9_-]{43}$`)

// NewToken returns a token and its hash. Only the hash is stored.
func NewToken() (token, hash string, err error) {
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", "", fmt.Errorf("approval token: %w", err)
	}
	token = TokenPrefix + base64.RawURLEncoding.EncodeToString(b[:])
	return token, HashToken(token), nil
}

// HashToken is the stored form of a token.
func HashToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// TokenExpiry is when a token minted now stops being valid.
func TokenExpiry(now, approvalExpires time.Time) time.Time {
	if t := now.Add(TokenTTL); t.Before(approvalExpires) {
		return t
	}
	return approvalExpires
}

// Approval is what the rules of an approval request need.
type Approval struct {
	ID             string
	Organization   string
	Project        string
	Tool           string
	ActionHash     string
	Status         string
	ExpiresAt      time.Time
	RequestedBy    string
	TokenHash      string
	TokenExpiresAt time.Time
}

// Effective is the status as of now: an open approval past its expiry is
// EXPIRED.
func (a Approval) Effective(now time.Time) string {
	if (a.Status == StatusPending || a.Status == StatusApproved) && !now.Before(a.ExpiresAt) {
		return StatusExpired
	}
	return a.Status
}

// Errors of the approval rules. The API maps each to its code.
var (
	ErrApprovalClosed      = errors.New("the approval request is no longer open")
	ErrApprovalNotApproved = errors.New("the action has not been approved")
	ErrApprovalExpired     = errors.New("the approval has expired")
	ErrApprovalUsed        = errors.New("the approval has already been used")
	ErrApprovalMismatch    = errors.New("the approval is for another action")
	ErrTokenInvalid        = errors.New("the approval token is not valid")
	ErrNotRequester        = errors.New("only the caller that asked for the approval may claim its token")
)

// CanDecide reports whether a person may approve or deny the request now.
func CanDecide(a Approval, now time.Time) error {
	if a.Effective(now) != StatusPending {
		return fmt.Errorf("%w (%s)", ErrApprovalClosed, a.Effective(now))
	}
	return nil
}

// CanClaim reports whether caller may claim a token for the approval now.
func CanClaim(a Approval, caller string, now time.Time) error {
	if caller != a.RequestedBy {
		return ErrNotRequester
	}
	switch a.Effective(now) {
	case StatusApproved:
		return nil
	case StatusExpired:
		return ErrApprovalExpired
	case StatusUsed:
		return ErrApprovalUsed
	default:
		return ErrApprovalNotApproved
	}
}

// CheckToken reports whether token lets the action with actionHash run now.
// A token that does not belong to the approval, or to another organization
// or project, is invalid; a used, closed or expired approval says so; a
// valid token for another action is a mismatch (and is not used up).
func CheckToken(a Approval, token string, org, project, tool, actionHash string, now time.Time) error {
	// No token minted (an empty hash) never compares equal: the lengths differ.
	if subtle.ConstantTimeCompare([]byte(HashToken(token)), []byte(a.TokenHash)) != 1 ||
		a.Organization != org || a.Project != project {
		return ErrTokenInvalid
	}
	switch a.Effective(now) {
	case StatusApproved:
	case StatusUsed:
		return ErrApprovalUsed
	case StatusExpired:
		return ErrApprovalExpired
	default:
		return ErrApprovalNotApproved
	}
	if !now.Before(a.TokenExpiresAt) {
		return ErrApprovalExpired
	}
	if a.Tool != tool || a.ActionHash != actionHash {
		return ErrApprovalMismatch
	}
	return nil
}
