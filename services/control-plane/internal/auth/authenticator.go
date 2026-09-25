package auth

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/golang-jwt/jwt/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/store"
)

// Mode is the human authentication mode.
type Mode string

const (
	ModeDev  Mode = "dev"
	ModeOIDC Mode = "oidc"
)

const sessionIssuer = "agenttwin-control-plane"
const sessionAudience = "agenttwin-api"

// SessionTTL is the lifetime of development session tokens.
const SessionTTL = 12 * time.Hour

// OrgHeader selects the organization when a user belongs to several.
const OrgHeader = "X-AgentTwin-Org"

// APIKeyHeader carries an API key when Authorization is not used.
const APIKeyHeader = "X-AgentTwin-Api-Key" //nolint:gosec // a header name, not a credential

// Store is the subset of the store used for authentication.
type Store interface {
	GetAPIKeyByPrefix(ctx context.Context, prefix string) (store.APIKey, error)
	TouchAPIKey(ctx context.Context, id string) error
	GetUser(ctx context.Context, id string) (store.User, error)
	GetUserByEmail(ctx context.Context, email string) (store.User, error)
	GetUserByOIDCSubject(ctx context.Context, subject string) (store.User, error)
	LinkOIDCSubject(ctx context.Context, userID, subject string) error
	Memberships(ctx context.Context, userID string) ([]store.Membership, error)
}

// Authenticator resolves request credentials to principals.
type Authenticator struct {
	Mode         Mode
	Store        Store
	Pepper       string
	sessionKey   []byte
	oidcVerifier *oidc.IDTokenVerifier
	now          func() time.Time
}

// Config configures an Authenticator.
type Config struct {
	Mode          Mode
	SessionSecret string
	Pepper        string
	OIDCIssuer    string
	OIDCClientID  string
	// OIDCVerifier overrides discovery (tests).
	OIDCVerifier *oidc.IDTokenVerifier
}

// New creates an Authenticator. In OIDC mode it performs discovery against the issuer.
func New(ctx context.Context, cfg Config, st Store) (*Authenticator, error) {
	a := &Authenticator{Mode: cfg.Mode, Store: st, Pepper: cfg.Pepper, sessionKey: []byte(cfg.SessionSecret), now: time.Now}
	if len(cfg.SessionSecret) < 32 {
		return nil, errors.New("session secret must be at least 32 bytes")
	}
	if cfg.Mode == ModeOIDC {
		switch {
		case cfg.OIDCVerifier != nil:
			a.oidcVerifier = cfg.OIDCVerifier
		case cfg.OIDCIssuer == "" || cfg.OIDCClientID == "":
			return nil, errors.New("OIDC mode requires OIDC_ISSUER_URL and OIDC_CLIENT_ID")
		default:
			provider, err := oidc.NewProvider(ctx, cfg.OIDCIssuer)
			if err != nil {
				return nil, fmt.Errorf("oidc discovery: %w", err)
			}
			a.oidcVerifier = provider.Verifier(&oidc.Config{ClientID: cfg.OIDCClientID})
		}
	}
	return a, nil
}

type sessionClaims struct {
	jwt.RegisteredClaims
	Org   string `json:"org"`
	Email string `json:"email"`
}

// IssueSession mints a development session token (AUTH_MODE=dev only).
func (a *Authenticator) IssueSession(user store.User, orgID string) (string, time.Time, error) {
	if a.Mode != ModeDev {
		return "", time.Time{}, errors.New("session issuing is only available in dev auth mode")
	}
	now := a.now()
	exp := now.Add(SessionTTL)
	tok, err := jwt.NewWithClaims(jwt.SigningMethodHS256, sessionClaims{
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer: sessionIssuer, Subject: user.ID, Audience: jwt.ClaimStrings{sessionAudience},
			IssuedAt: jwt.NewNumericDate(now), ExpiresAt: jwt.NewNumericDate(exp),
		},
		Org: orgID, Email: user.Email,
	}).SignedString(a.sessionKey)
	return tok, exp, err
}

func (a *Authenticator) verifySession(tok string) (sessionClaims, error) {
	var c sessionClaims
	parsed, err := jwt.ParseWithClaims(tok, &c, func(t *jwt.Token) (any, error) { return a.sessionKey, nil },
		jwt.WithIssuer(sessionIssuer), jwt.WithAudience(sessionAudience), jwt.WithExpirationRequired(),
		jwt.WithValidMethods([]string{jwt.SigningMethodHS256.Alg()}), jwt.WithTimeFunc(a.now))
	if err != nil || !parsed.Valid {
		return c, fmt.Errorf("invalid session: %w", err)
	}
	return c, nil
}

// VerifiedKey is the result of API key verification.
type VerifiedKey struct {
	Key store.APIKey
}

// ErrInvalidCredentials hides which part of a credential was wrong.
var ErrInvalidCredentials = errors.New("invalid credentials")

// VerifyAPIKey checks a plaintext key: format, hash, revocation, expiry.
func (a *Authenticator) VerifyAPIKey(ctx context.Context, plaintext string) (store.APIKey, error) {
	prefix, secret, err := ParseAPIKey(plaintext)
	if err != nil {
		return store.APIKey{}, ErrInvalidCredentials
	}
	k, err := a.Store.GetAPIKeyByPrefix(ctx, prefix)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return store.APIKey{}, ErrInvalidCredentials
		}
		return store.APIKey{}, err
	}
	if !VerifySecret(a.Pepper, secret, k.SecretHash) {
		return store.APIKey{}, ErrInvalidCredentials
	}
	if k.RevokedAt != nil {
		return store.APIKey{}, ErrInvalidCredentials
	}
	if k.ExpiresAt != nil && !a.now().Before(*k.ExpiresAt) {
		return store.APIKey{}, ErrInvalidCredentials
	}
	_ = a.Store.TouchAPIKey(ctx, k.ID)
	return k, nil
}

// Authenticate resolves the request's credential. It returns
// httpx.ErrUnauthorized when no valid credential is present.
func (a *Authenticator) Authenticate(r *http.Request) (authn.Principal, error) {
	ctx := r.Context()
	cred := authn.BearerToken(r)
	if cred == "" {
		cred = strings.TrimSpace(r.Header.Get(APIKeyHeader))
	}
	if cred == "" {
		return authn.Principal{}, httpx.ErrUnauthorized
	}
	if LooksLikeAPIKey(cred) {
		k, err := a.VerifyAPIKey(ctx, cred)
		if err != nil {
			if errors.Is(err, ErrInvalidCredentials) {
				return authn.Principal{}, httpx.ErrUnauthorized
			}
			return authn.Principal{}, err
		}
		return authn.Principal{OrgID: k.OrganizationID, Actor: "apikey:" + k.ID, Role: authn.RoleAPIKey,
			ProjectIDs: []string{k.ProjectID}, Scopes: k.Scopes}, nil
	}
	var user store.User
	var tokenOrg string
	switch a.Mode {
	case ModeDev:
		c, err := a.verifySession(cred)
		if err != nil {
			return authn.Principal{}, httpx.ErrUnauthorized
		}
		u, err := a.Store.GetUser(ctx, c.Subject)
		if err != nil {
			return authn.Principal{}, httpx.ErrUnauthorized
		}
		user, tokenOrg = u, c.Org
	case ModeOIDC:
		idt, err := a.oidcVerifier.Verify(ctx, cred)
		if err != nil {
			return authn.Principal{}, httpx.ErrUnauthorized
		}
		var claims struct {
			Email         string `json:"email"`
			EmailVerified *bool  `json:"email_verified"`
		}
		_ = idt.Claims(&claims)
		u, err := a.Store.GetUserByOIDCSubject(ctx, idt.Subject)
		if err != nil {
			// First login: link by verified email to a pre-provisioned user.
			if claims.Email == "" || (claims.EmailVerified != nil && !*claims.EmailVerified) {
				return authn.Principal{}, httpx.ErrUnauthorized
			}
			u, err = a.Store.GetUserByEmail(ctx, claims.Email)
			if err != nil {
				return authn.Principal{}, httpx.ErrUnauthorized
			}
			if err := a.Store.LinkOIDCSubject(ctx, u.ID, idt.Subject); err != nil {
				return authn.Principal{}, err
			}
		}
		user = u
	default:
		return authn.Principal{}, httpx.ErrUnauthorized
	}
	if user.DisabledAt != nil {
		return authn.Principal{}, httpx.ErrUnauthorized
	}
	mems, err := a.Store.Memberships(ctx, user.ID)
	if err != nil {
		return authn.Principal{}, err
	}
	if len(mems) == 0 {
		return authn.Principal{}, httpx.ErrForbidden
	}
	want := r.Header.Get(OrgHeader)
	if want == "" {
		want = tokenOrg
	}
	chosen := mems[0]
	if want != "" {
		found := false
		for _, m := range mems {
			if m.OrganizationID == want {
				chosen, found = m, true
				break
			}
		}
		if !found {
			return authn.Principal{}, httpx.ErrForbidden
		}
	}
	return authn.Principal{OrgID: chosen.OrganizationID, Actor: "user:" + user.ID, Email: user.Email, Role: chosen.Role, AllProjects: true}, nil
}

// Middleware authenticates every request except the listed public paths.
func (a *Authenticator) Middleware(public func(*http.Request) bool) httpx.Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if public != nil && public(r) {
				next.ServeHTTP(w, r)
				return
			}
			p, err := a.Authenticate(r)
			if err != nil {
				httpx.WriteError(w, r, err)
				return
			}
			next.ServeHTTP(w, r.WithContext(authn.WithPrincipal(r.Context(), p)))
		})
	}
}

// SetClock overrides the clock (tests).
func (a *Authenticator) SetClock(now func() time.Time) { a.now = now }
