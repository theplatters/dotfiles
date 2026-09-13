use serde_json::Value;

/// Byte budget for a single displayed history message. Mirrors
/// `MAX_MESSAGE_TEXT` in state.rs; duplicated here so history accumulation
/// stays bounded even before state-level truncation.
pub const HISTORY_TEXT_BUDGET: usize = 64 * 1024;

/// Return the largest index `<= index` that is a UTF-8 char boundary in `s`.
pub fn floor_char_boundary(s: &str, index: usize) -> usize {
    let mut i = index.min(s.len());
    while i > 0 && !s.is_char_boundary(i) {
        i -= 1;
    }
    i
}

/// Return the smallest index `>= index` that is a UTF-8 char boundary.
pub fn ceil_char_boundary(s: &str, index: usize) -> usize {
    let mut i = index.min(s.len());
    while i < s.len() && !s.is_char_boundary(i) {
        i += 1;
    }
    i
}

/// Keep at most `max_bytes` leading bytes, never splitting a char.
pub fn truncate_head(s: &str, max_bytes: usize) -> String {
    if s.len() <= max_bytes {
        return s.to_string();
    }
    let cut = floor_char_boundary(s, max_bytes);
    s[..cut].to_string()
}

/// Keep at most `max_bytes` trailing bytes, never splitting a char.
pub fn truncate_tail(s: &str, max_bytes: usize) -> String {
    if s.len() <= max_bytes {
        return s.to_string();
    }
    let start = ceil_char_boundary(s, s.len() - max_bytes);
    s[start..].to_string()
}

/// Mirrors legacy textFromMessage: string content verbatim, otherwise the
/// concatenation of {type:"text", text} parts. Thinking blocks, tool
/// calls/results and images are never searchable conversation text.
pub fn text_from_message(message: &Value) -> String {
    if message.is_null() {
        return String::new();
    }
    if let Some(s) = message.get("content").and_then(|v| v.as_str()) {
        // Bound untrusted agent content before it accumulates in the caller.
        return truncate_head(s, HISTORY_TEXT_BUDGET);
    }
    let mut out = String::new();
    if let Some(parts) = message.get("content").and_then(|v| v.as_array()) {
        for part in parts {
            if part.get("type").and_then(|v| v.as_str()) == Some("text") {
                if let Some(t) = part.get("text").and_then(|v| v.as_str()) {
                    // Bound accumulation part-by-part so a huge parts array
                    // cannot OOM before state-level truncation.
                    let remaining = HISTORY_TEXT_BUDGET.saturating_sub(out.len());
                    if remaining == 0 {
                        break;
                    }
                    if t.len() <= remaining {
                        out.push_str(t);
                    } else {
                        out.push_str(&truncate_head(t, remaining));
                        break;
                    }
                }
            }
        }
    }
    out
}

#[derive(Debug, Clone)]
pub struct HistoryMsg {
    pub role: String,
    pub text: String,
    pub timestamp: i64,
    pub id: Value,
}

/// Mirrors legacy normalizeMessages exactly.
pub fn normalize_messages(values: &Value) -> Vec<HistoryMsg> {
    let mut out = Vec::new();
    let arr = match values.as_array() {
        Some(a) => a,
        None => return out,
    };
    for message in arr {
        let role = message.get("role").and_then(|v| v.as_str()).unwrap_or("");
        if role != "user" && role != "assistant" {
            continue;
        }
        let text = text_from_message(message);
        if text.trim().is_empty() {
            continue;
        }
        let timestamp = message
            .get("timestamp")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let id = message
            .get("id")
            .cloned()
            .unwrap_or(Value::from(out.len() as u64));
        out.push(HistoryMsg {
            role: role.to_string(),
            text,
            timestamp,
            id,
        });
    }
    out
}

pub fn history_msg_to_value(m: &HistoryMsg) -> Value {
    serde_json::json!({
        "role": m.role,
        "text": m.text,
        "timestamp": m.timestamp,
        "id": m.id,
    })
}
