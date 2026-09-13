use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Bounded framing: 4 MiB per JSONL line. Exact approvals can approach
/// ~1 MiB escaped, so 4 MiB leaves headroom without unbounded reads.
pub const MAX_LINE_BYTES: usize = 4 * 1024 * 1024;

#[derive(Debug, Clone, Deserialize)]
pub struct UiInput {
    pub version: Option<i64>,
    pub id: Option<String>,
    #[serde(default)]
    pub op: String,
    #[serde(default)]
    pub args: Option<Value>,
}

#[derive(Debug, Clone, Serialize)]
pub struct UiEvent {
    pub name: String,
    pub args: Vec<Value>,
}

impl UiEvent {
    pub fn new(name: &str, args: Vec<Value>) -> Self {
        Self {
            name: name.to_string(),
            args,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct BridgeUpdate {
    pub version: u8,
    #[serde(rename = "type")]
    pub kind: String,
    pub state: serde_json::Value,
    pub events: Vec<UiEvent>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ack: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub accepted: Option<bool>,
}
