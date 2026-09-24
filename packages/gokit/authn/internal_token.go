package authn

import (
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/logx"
)

// InternalIssuer is the issuer of internal service tokens.
const InternalIssuer = "agenttwin-internal"

// InternalTokenTTL bounds the replay window of a leaked internal token.
const InternalTokenTTL = 60 * time.Second

type internalClaims struct {
	jwt.RegisteredClaims
	Org         string   `json:"org"`
	Role        Role     `json:"role"`
	Email       string   `json:"email,omitempty"`
	Projects    []string `json:"projects,omitempty"`
	AllProjects bool     `json:"all_projects,omitempty"`
	Scopes      []Scope  `json:"scopes,omitempty"`
	RequestID   string   `json:"rid,omitempty"`
}

// TokenService mints and verifies internal tokens with a shared HMAC key.
type TokenService struct {
	key []byte
	now func() time.Time
}

// NewTokenService creates a TokenService. The secret must be at least 32 bytes.
func NewTokenService(secret string) (*TokenService, error) {
	if len(secret) < 32 {
		return nil, errors.New("internal token secret must be at least 32 bytes")
	}
	return &TokenService{key: []byte(secret), now: time.Now}, nil
}

// SetClock overrides the clock (tests).
func (t *TokenService) SetClock(now func() time.Time) { t.now = now }

// Mint issues a token for the principal addressed to audience (target service).
func (t *TokenService) Mint(p Principal, audience, requestID string) (string, error) {
	if p.OrgID == "" || p.Actor == "" || p.Role == "" {
		return "", errors.New("mint: principal requires org, actor and role")
	}
	now := t.now()
	claims := internalClaims{
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer:    InternalIssuer,
			Subject:   p.Actor,
			Audience:  jwt.ClaimStrings{audience},
			IssuedAt:  jwt.NewNumericDate(now),
			NotBefore: jwt.NewNumericDate(now.Add(-5 * time.Second)),
			ExpiresAt: jwt.NewNumericDate(now.Add(InternalTokenTTL)),
			ID:        ids.New(),
		},
		Org:         p.OrgID,
		Role:        p.Role,
		Email:       p.Email,
		Projects:    p.ProjectIDs,
		AllProjects: p.AllProjects,
		Scopes:      p.Scopes,
		RequestID:   requestID,
	}
	return jwt.NewWithClaims(jwt.SigningMethodHS256, claims).SignedString(t.key)
}

// Verify validates signature, issuer, audience and expiry and returns the principal.
func (t *TokenService) Verify(token, audience string) (Principal, string, error) {
	var claims internalClaims
	parsed, err := jwt.ParseWithClaims(token, &claims, func(tok *jwt.Token) (any, error) {
		if tok.Method != jwt.SigningMethodHS256 {
			return nil, fmt.Errorf("unexpected signing method %v", tok.Header["alg"])
		}
		return t.key, nil
	},
		jwt.WithIssuer(InternalIssuer),
		jwt.WithAudience(audience),
		jwt.WithExpirationRequired(),
		jwt.WithTimeFunc(t.now),
		jwt.WithValidMethods([]string{jwt.SigningMethodHS256.Alg()}),
	)
	if err != nil || !parsed.Valid {
		return Principal{}, "", fmt.Errorf("invalid internal token: %w", err)
	}
	if claims.Org == "" || claims.Subject == "" || claims.Role == "" {
		return Principal{}, "", errors.New("invalid internal token: missing claims")
	}
	p := Principal{
		OrgID:       claims.Org,
		Actor:       claims.Subject,
		Email:       claims.Email,
		Role:        claims.Role,
		ProjectIDs:  claims.Projects,
		AllProjects: claims.AllProjects,
		Scopes:      claims.Scopes,
	}
	return p, claims.RequestID, nil
}

// BearerToken extracts a bearer token from the Authorization header.
func BearerToken(r *http.Request) string {
	h := r.Header.Get("Authorization")
	if len(h) > 7 && strings.EqualFold(h[:7], "bearer ") {
		return strings.TrimSpace(h[7:])
	}
	return ""
}

// RequireInternal authenticates internal service calls for the given audience.
// Health and metrics endpoints are exempt.
func RequireInternal(ts *TokenService, audience string) httpx.Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if strings.HasPrefix(r.URL.Path, "/health/") || r.URL.Path == "/metrics" {
				next.ServeHTTP(w, r)
				return
			}
			tok := BearerToken(r)
			if tok == "" {
				httpx.WriteError(w, r, httpx.ErrUnauthorized)
				return
			}
			p, rid, err := ts.Verify(tok, audience)
			if err != nil {
				httpx.WriteError(w, r, httpx.ErrUnauthorized)
				return
			}
			ctx := WithPrincipal(r.Context(), p)
			if rid != "" && logx.RequestID(ctx) == "" {
				ctx = logx.WithRequestID(ctx, rid)
			}
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// Require returns the principal if it holds perm, or an API error.
func Require(r *http.Request, perm Permission) (Principal, error) {
	p, ok := FromContext(r.Context())
	if !ok {
		return Principal{}, httpx.ErrUnauthorized
	}
	if !p.Can(perm) {
		return Principal{}, httpx.ErrForbidden.WithDetails(map[string]any{"required_permission": string(perm)})
	}
	return p, nil
}

// RequireProject checks perm and project access. Access to a project of another
// organization is reported as NOT_FOUND so identifiers cannot be probed.
func RequireProject(r *http.Request, perm Permission, projectID string) (Principal, error) {
	p, err := Require(r, perm)
	if err != nil {
		return p, err
	}
	if !p.CanAccessProject(projectID) {
		return Principal{}, httpx.ErrNotFound
	}
	return p, nil
}

// ServicePrincipal builds a principal for event-driven service-to-service calls.
func ServicePrincipal(service, orgID string, projectIDs ...string) Principal {
	return Principal{OrgID: orgID, Actor: "service:" + service, Role: RoleService, ProjectIDs: projectIDs}
}
