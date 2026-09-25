package gateway

import (
	"net/http"
	"time"
)

// Idempotency record states.
const (
	IdemInProgress = "IN_PROGRESS"
	IdemCompleted  = "COMPLETED"
	// IdemUnknown is a call whose effect is unknown: it timed out, the tool
	// failed or could not be reached. A retry is forwarded again, with the
	// key, for the tool to deduplicate.
	IdemUnknown = "UNKNOWN"
)

// StaleAfter is how long a call may stay in progress before a retry takes
// over: longer than any tool timeout, so only a gateway that died mid-call
// leaves one behind.
const StaleAfter = 2 * time.Minute

// IdemRecord is what is stored under an idempotency key.
type IdemRecord struct {
	ActionHash string
	State      string
	UpdatedAt  time.Time
}

// IdemVerdict is what a call with a known key does.
type IdemVerdict string

// Verdicts.
const (
	// IdemProceed: no record, or the previous outcome is unknown.
	IdemProceed IdemVerdict = "proceed"
	// IdemReplay: the same action completed; its stored response is returned.
	IdemReplay IdemVerdict = "replay"
	// IdemReused: the key was used for another action.
	IdemReused IdemVerdict = "reused"
	// IdemBusy: the same action is still in flight.
	IdemBusy IdemVerdict = "in_progress"
)

// CheckIdempotency decides a call against the record of its key (nil when
// the key is new).
func CheckIdempotency(rec *IdemRecord, actionHash string, now time.Time) IdemVerdict {
	switch {
	case rec == nil:
		return IdemProceed
	case rec.ActionHash != actionHash:
		return IdemReused
	case rec.State == IdemCompleted:
		return IdemReplay
	case rec.State == IdemInProgress && now.Sub(rec.UpdatedAt) < StaleAfter:
		return IdemBusy
	default:
		return IdemProceed
	}
}

// StateAfter is the state a forwarded call leaves its key in. The tool
// answered: completed, unless it failed on its side, timed out or asked to
// retry later, in which case the effect is unknown.
func StateAfter(status int, err error) string {
	if err != nil || status >= 500 || status == http.StatusRequestTimeout || status == http.StatusTooManyRequests {
		return IdemUnknown
	}
	return IdemCompleted
}
