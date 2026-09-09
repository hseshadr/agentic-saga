CREATE TABLE sagas (
  saga_id TEXT PRIMARY KEY CHECK (length(saga_id) BETWEEN 21 AND 69),
  saga_seq INTEGER NOT NULL CHECK (saga_seq >= 1),
  status TEXT NOT NULL CHECK (status IN (
    'created', 'running', 'recovery_plan_required', 'retry_wait',
    'reconciling_unknown', 'compensating', 'human_required',
    'succeeded_verified', 'compensated_verified', 'aborted_clean',
    'resolved_with_exception'
  )),
  definition_version TEXT NOT NULL CHECK (length(definition_version) BETWEEN 1 AND 200),
  projection_json BLOB NOT NULL CHECK (typeof(projection_json) = 'blob'),
  fence_token INTEGER NOT NULL DEFAULT 0 CHECK (fence_token >= 0),
  lease_owner TEXT,
  lease_expires_at TEXT,
  CHECK (
    (lease_owner IS NULL AND lease_expires_at IS NULL)
    OR (
      fence_token >= 1
      AND lease_owner IS NOT NULL
      AND length(lease_owner) BETWEEN 1 AND 200
      AND lease_expires_at IS NOT NULL
      AND julianday(lease_expires_at) IS NOT NULL
    )
  )
);

CREATE TABLE ledger_events (
  event_id TEXT PRIMARY KEY CHECK (length(event_id) BETWEEN 20 AND 68),
  saga_id TEXT NOT NULL REFERENCES sagas(saga_id),
  saga_seq INTEGER NOT NULL CHECK (saga_seq >= 1),
  event_type TEXT NOT NULL CHECK (event_type IN (
    'saga_created', 'saga_started', 'agent_turn_reserved', 'agent_turn_failed',
    'read_started', 'read_observed', 'read_unavailable', 'effect_intent_recorded',
    'dispatch_started', 'dispatch_aborted_before_entry', 'effect_outcome_recorded',
    'reconciliation_recorded',
    'recovery_plan_required',
    'recovery_plan_accepted', 'recovery_plan_rejected', 'compensation_started',
    'compensation_intent_recorded', 'invariant_evaluated', 'human_required',
    'human_resolution_recorded', 'terminal_assigned', 'proposal_rejected',
    'terminal_denied', 'approval_consumed'
  )),
  event_json BLOB NOT NULL CHECK (typeof(event_json) = 'blob' AND length(event_json) > 1),
  event_hash TEXT NOT NULL CHECK (
    length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'
  ),
  prior_hash TEXT CHECK (
    prior_hash IS NULL
    OR (length(prior_hash) = 64 AND prior_hash NOT GLOB '*[^0-9a-f]*')
  ),
  UNIQUE (saga_id, saga_seq),
  CHECK (
    (saga_seq = 1 AND prior_hash IS NULL)
    OR (saga_seq > 1 AND prior_hash IS NOT NULL)
  )
);

CREATE TABLE outbox_commands (
  command_id TEXT PRIMARY KEY CHECK (length(command_id) BETWEEN 20 AND 68),
  saga_id TEXT NOT NULL REFERENCES sagas(saga_id),
  operation_id TEXT NOT NULL UNIQUE CHECK (length(operation_id) = 67),
  tool_name TEXT NOT NULL CHECK (length(tool_name) BETWEEN 1 AND 200),
  definition_version TEXT NOT NULL CHECK (length(definition_version) BETWEEN 1 AND 200),
  command_schema_version TEXT NOT NULL CHECK (length(command_schema_version) BETWEEN 1 AND 200),
  step_instance_id TEXT NOT NULL CHECK (length(step_instance_id) BETWEEN 13 AND 69),
  direction TEXT NOT NULL CHECK (direction IN ('forward', 'compensation')),
  semantic_generation INTEGER NOT NULL CHECK (semantic_generation >= 0),
  command_json BLOB NOT NULL CHECK (typeof(command_json) = 'blob' AND length(command_json) > 1),
  command_hash TEXT NOT NULL CHECK (
    length(command_hash) = 64 AND command_hash NOT GLOB '*[^0-9a-f]*'
  ),
  capabilities_json BLOB,
  capability_digest TEXT CHECK (
    capability_digest IS NULL OR (
      length(capability_digest) = 64 AND capability_digest NOT GLOB '*[^0-9a-f]*'
    )
  ),
  state TEXT NOT NULL CHECK (state IN (
    'runnable', 'claimed', 'completed', 'parked', 'suspended_for_human'
  )),
  available_at TEXT NOT NULL CHECK (
    length(available_at) >= 20 AND julianday(available_at) IS NOT NULL
  ),
  claim_id TEXT,
  claim_owner TEXT,
  claim_expires_at TEXT,
  claim_generation INTEGER NOT NULL DEFAULT 0 CHECK (claim_generation >= 0),
  claim_fence_token INTEGER NOT NULL DEFAULT 0 CHECK (claim_fence_token >= 0),
  delivery_attempt INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempt >= 0),
  suspended_at_seq INTEGER CHECK (suspended_at_seq IS NULL OR suspended_at_seq >= 1),
  CHECK (
    (capabilities_json IS NULL AND capability_digest IS NULL)
    OR (typeof(capabilities_json) = 'blob' AND capability_digest IS NOT NULL)
  ),
  CHECK (
    (
      state = 'runnable'
      AND claim_id IS NULL
      AND claim_owner IS NULL
      AND claim_expires_at IS NULL
      AND suspended_at_seq IS NULL
      AND (
        (claim_generation = 0 AND claim_fence_token = 0 AND delivery_attempt = 0)
        OR (claim_generation >= 1 AND delivery_attempt >= 1)
      )
    )
    OR (
      state = 'suspended_for_human'
      AND claim_id IS NULL
      AND claim_owner IS NULL
      AND claim_expires_at IS NULL
      AND suspended_at_seq IS NOT NULL
      AND (
        (claim_generation = 0 AND claim_fence_token = 0 AND delivery_attempt = 0)
        OR (claim_generation >= 1 AND claim_fence_token >= 1 AND delivery_attempt >= 1)
      )
    )
    OR (
      state IN ('claimed', 'completed', 'parked')
      AND claim_id IS NOT NULL
      AND length(claim_id) = 38
      AND substr(claim_id, 1, 6) = 'claim_'
      AND substr(claim_id, 7) NOT GLOB '*[^0-9a-f]*'
      AND claim_owner IS NOT NULL
      AND length(claim_owner) BETWEEN 1 AND 200
      AND claim_expires_at IS NOT NULL
      AND julianday(claim_expires_at) IS NOT NULL
      AND claim_generation >= 1
      AND delivery_attempt >= 1
      AND suspended_at_seq IS NULL
    )
  )
);

CREATE TABLE transition_receipts (
  transition_id TEXT PRIMARY KEY CHECK (length(transition_id) BETWEEN 20 AND 68),
  saga_id TEXT NOT NULL REFERENCES sagas(saga_id),
  expected_seq INTEGER NOT NULL CHECK (expected_seq >= 1),
  resulting_seq INTEGER NOT NULL CHECK (resulting_seq > expected_seq),
  payload_digest TEXT NOT NULL CHECK (
    length(payload_digest) = 64 AND payload_digest NOT GLOB '*[^0-9a-f]*'
  ),
  request_digest TEXT CHECK (
    request_digest IS NULL OR (
      length(request_digest) = 64 AND request_digest NOT GLOB '*[^0-9a-f]*'
    )
  ),
  transition_kind TEXT NOT NULL DEFAULT 'standard' CHECK (transition_kind IN (
    'standard', 'proposal', 'human_suspension', 'human_resolution', 'terminal', 'reconciliation'
  )),
  projection_json BLOB NOT NULL CHECK (typeof(projection_json) = 'blob'),
  transition_json BLOB NOT NULL CHECK (
    typeof(transition_json) = 'blob' AND length(transition_json) > 1
  ),
  UNIQUE (saga_id, expected_seq)
);

CREATE TABLE reconciliation_jobs (
  job_id TEXT PRIMARY KEY CHECK (
    length(job_id) = 70 AND substr(job_id, 1, 6) = 'recon_'
    AND substr(job_id, 7) NOT GLOB '*[^0-9a-f]*'
  ),
  command_id TEXT NOT NULL UNIQUE REFERENCES outbox_commands(command_id),
  saga_id TEXT NOT NULL REFERENCES sagas(saga_id),
  operation_id TEXT NOT NULL UNIQUE REFERENCES outbox_commands(operation_id),
  state TEXT NOT NULL CHECK (state IN (
    'due', 'waiting', 'claimed', 'completed', 'requeued', 'human_required'
  )),
  due_at TEXT NOT NULL CHECK (julianday(due_at) IS NOT NULL),
  first_dispatch_at TEXT NOT NULL CHECK (julianday(first_dispatch_at) IS NOT NULL),
  claim_id TEXT,
  claim_owner TEXT,
  claim_expires_at TEXT,
  claim_generation INTEGER NOT NULL DEFAULT 0 CHECK (claim_generation >= 0),
  claim_fence_token INTEGER NOT NULL DEFAULT 0 CHECK (claim_fence_token >= 0),
  recovery_policy_json BLOB,
  recovery_policy_digest TEXT CHECK (
    recovery_policy_digest IS NULL OR (
      length(recovery_policy_digest) = 64
      AND recovery_policy_digest NOT GLOB '*[^0-9a-f]*'
    )
  ),
  claimed_at TEXT CHECK (claimed_at IS NULL OR julianday(claimed_at) IS NOT NULL),
  lookup_attempt INTEGER NOT NULL DEFAULT 0 CHECK (lookup_attempt >= 0),
  lookup_started_at TEXT CHECK (
    lookup_started_at IS NULL OR julianday(lookup_started_at) IS NOT NULL
  ),
  CHECK (
    (recovery_policy_json IS NULL AND recovery_policy_digest IS NULL)
    OR (typeof(recovery_policy_json) = 'blob' AND recovery_policy_digest IS NOT NULL)
  ),
  CHECK (
    (claim_generation = 0 AND claimed_at IS NULL)
    OR (claim_generation >= 1 AND claimed_at IS NOT NULL)
  ),
  CHECK (
    (lookup_attempt = 0 AND lookup_started_at IS NULL)
    OR (lookup_attempt >= 1 AND lookup_started_at IS NOT NULL)
  ),
  CHECK (
    (state = 'claimed' AND claim_id IS NOT NULL AND claim_owner IS NOT NULL
      AND claim_expires_at IS NOT NULL AND claim_generation >= 1)
    OR (state != 'claimed' AND claim_id IS NULL AND claim_owner IS NULL
      AND claim_expires_at IS NULL)
  )
);

CREATE INDEX ledger_events_replay
  ON ledger_events (saga_id, saga_seq);

CREATE INDEX outbox_runnable_scan
  ON outbox_commands (available_at, command_id)
  WHERE state = 'runnable';

CREATE INDEX outbox_expired_claim_scan
  ON outbox_commands (claim_expires_at, command_id)
  WHERE state = 'claimed';

CREATE INDEX reconciliation_due_scan
  ON reconciliation_jobs (due_at, job_id)
  WHERE state IN ('due', 'waiting');

CREATE INDEX reconciliation_expired_claim_scan
  ON reconciliation_jobs (claim_expires_at, job_id)
  WHERE state = 'claimed';

CREATE TRIGGER ledger_events_no_update
BEFORE UPDATE ON ledger_events
BEGIN
  SELECT RAISE(ABORT, 'ledger_events is append-only');
END;

CREATE TRIGGER ledger_events_no_delete
BEFORE DELETE ON ledger_events
BEGIN
  SELECT RAISE(ABORT, 'ledger_events is append-only');
END;

CREATE TRIGGER transition_receipts_no_update
BEFORE UPDATE ON transition_receipts
BEGIN
  SELECT RAISE(ABORT, 'transition_receipts is append-only');
END;

CREATE TRIGGER transition_receipts_no_delete
BEFORE DELETE ON transition_receipts
BEGIN
  SELECT RAISE(ABORT, 'transition_receipts is append-only');
END;

CREATE TRIGGER outbox_claim_fence_guard
BEFORE UPDATE OF claim_fence_token ON outbox_commands
WHEN NOT (
  (
    OLD.state IN ('runnable', 'claimed')
    AND NEW.state = 'claimed'
    AND NEW.claim_generation = OLD.claim_generation + 1
    AND NEW.delivery_attempt = OLD.delivery_attempt
      + CASE WHEN OLD.state = 'runnable' THEN 1 ELSE 0 END
    AND (
      OLD.state = 'runnable'
      OR julianday(NEW.claim_expires_at) > julianday(OLD.claim_expires_at)
    )
  )
  OR (
    OLD.state = 'claimed'
    AND NEW.state = 'runnable'
    AND NEW.claim_id IS NULL
    AND NEW.claim_owner IS NULL
    AND NEW.claim_expires_at IS NULL
    AND (
      (NEW.claim_generation = 0 AND NEW.claim_fence_token = 0 AND NEW.delivery_attempt = 0)
      OR (
        NEW.claim_generation = OLD.claim_generation
        AND NEW.claim_fence_token = OLD.claim_fence_token
        AND NEW.delivery_attempt = OLD.delivery_attempt - 1
        AND NEW.delivery_attempt >= 1
      )
    )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'historical outbox claim fence is immutable');
END;

CREATE TRIGGER sagas_fence_never_decreases
BEFORE UPDATE OF fence_token ON sagas
WHEN NEW.fence_token < OLD.fence_token
BEGIN
  SELECT RAISE(ABORT, 'Saga fence token must not decrease');
END;

PRAGMA user_version = 2;
