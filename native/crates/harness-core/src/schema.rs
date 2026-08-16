//! Versioned envelope framing for canonical payloads.
//!
//! Every canonical payload travels inside an envelope carrying the schema
//! version and a semantic kind tag, so readers can validate before
//! interpreting. JSON is the v1 carrier; the envelope itself is the
//! stable seam for future carriers.

use crate::types::CANONICAL_SCHEMA;
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Envelope<T> {
    pub schema: u32,
    pub kind: String,
    pub payload: T,
}

pub fn envelope<T>(kind: impl Into<String>, payload: T) -> Envelope<T> {
    Envelope {
        schema: CANONICAL_SCHEMA,
        kind: kind.into(),
        payload,
    }
}

pub fn current_schema() -> u32 {
    CANONICAL_SCHEMA
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::SessionState;

    #[test]
    fn envelope_round_trip_preserves_schema_and_kind() {
        let envelope = envelope(
            "session",
            serde_json::json!({"id": "abc", "state": "open"}),
        );
        let encoded = serde_json::to_string(&envelope).unwrap();
        let decoded: Envelope<serde_json::Value> = serde_json::from_str(&encoded).unwrap();
        assert_eq!(decoded.schema, CANONICAL_SCHEMA);
        assert_eq!(decoded.kind, "session");
        assert_eq!(decoded.payload["id"], "abc");
    }

    #[test]
    fn session_type_round_trip() {
        let session = crate::types::Session {
            id: "cc2ddfdeb9da4043b29892e85956141c".into(),
            title: "smoke".into(),
            state: SessionState::Open,
            created_at: 1786862486.346,
            updated_at: 1786862486.346,
            metadata_json: "{}".into(),
        };
        let encoded = serde_json::to_string(&session).unwrap();
        let decoded: crate::types::Session = serde_json::from_str(&encoded).unwrap();
        assert_eq!(session, decoded);
    }

    #[test]
    fn unknown_fields_are_rejected_not_silently_dropped() {
        // Strictness: a payload with an unknown field must fail to parse,
        // so schema drift is loud instead of silent.
        let encoded = r#"{"id":"x","title":"t","state":"open","created_at":1.0,"updated_at":1.0,"metadata_json":"{}","bogus":true}"#;
        let result: Result<crate::types::Session, _> = serde_json::from_str(encoded);
        assert!(result.is_err());
    }
}