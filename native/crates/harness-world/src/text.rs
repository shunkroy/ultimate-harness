//! Shared offline text utilities: normalization, tokenization, fuzzy
//! distance. Deterministic, dependency-light, Unicode-aware baseline.

use unicode_normalization::UnicodeNormalization;

/// Normalize text for matching: NFC, lowercase, ligature and accent
/// folding, punctuation stripping, whitespace collapse.
pub fn normalize(input: &str) -> String {
    let nfc: String = input.nfc().collect();
    let mut out = String::with_capacity(nfc.len());
    let mut last_space = false;
    for ch in nfc.chars() {
        if ch.is_alphanumeric() || ch == '\'' || ch == '’' {
            for folded in fold_char(ch) {
                out.push(folded);
            }
            last_space = false;
        } else if ch.is_whitespace() {
            if !last_space && !out.is_empty() {
                out.push(' ');
            }
            last_space = true;
        } else {
            // punctuation etc. acts as a word boundary
            if !last_space && !out.is_empty() {
                out.push(' ');
            }
            last_space = true;
        }
    }
    out.trim().to_string()
}

/// Canonical key: normalized, with leading articles stripped and the
/// curly apostrophe folded. Used for index keys and lookup queries.
pub fn canonical(input: &str) -> String {
    let normalized = normalize(input);
    let lower = normalized.to_lowercase();
    let stripped = if let Some(rest) = lower.strip_prefix("the ") {
        rest
    } else if let Some(rest) = lower.strip_prefix("a ") {
        rest
    } else if let Some(rest) = lower.strip_prefix("an ") {
        rest
    } else {
        &lower
    };
    stripped.trim().to_string()
}

fn fold_char(ch: char) -> Vec<char> {
    match ch.to_lowercase().next().unwrap_or(ch) {
        'ﬁ' => vec!['f', 'i'],
        'ﬂ' => vec!['f', 'l'],
        'ﬀ' => vec!['f', 'f'],
        'ﬃ' => vec!['f', 'f', 'i'],
        'ﬄ' => vec!['f', 'f', 'l'],
        'ﬅ' => vec!['f', 't'],
        'ﬆ' => vec!['s', 't'],
        'æ' => vec!['a', 'e'],
        'œ' => vec!['o', 'e'],
        'ß' => vec!['s', 's'],
        'à' | 'á' | 'â' | 'ã' | 'ä' | 'å' | 'ā' | 'ă' | 'ą' => vec!['a'],
        'è' | 'é' | 'ê' | 'ë' | 'ē' | 'ĕ' | 'ė' | 'ę' => vec!['e'],
        'ì' | 'í' | 'î' | 'ï' | 'ī' | 'ĭ' | 'į' => vec!['i'],
        'ò' | 'ó' | 'ô' | 'õ' | 'ö' | 'ō' | 'ŏ' | 'ő' => vec!['o'],
        'ù' | 'ú' | 'û' | 'ü' | 'ū' | 'ŭ' | 'ů' | 'ű' | 'ų' => vec!['u'],
        'ç' | 'ć' | 'č' => vec!['c'],
        'ñ' | 'ń' | 'ň' => vec!['n'],
        'š' => vec!['s'],
        'ž' => vec!['z'],
        'ÿ' | 'ý' => vec!['y'],
        'đ' => vec!['d'],
        'ł' => vec!['l'],
        other => vec![other],
    }
}

/// Tokenize normalized text on whitespace.
pub fn tokenize(input: &str) -> Vec<String> {
    normalize(input)
        .split(' ')
        .filter(|token| !token.is_empty())
        .map(str::to_string)
        .collect()
}

/// Levenshtein edit distance (used for typo tolerance).
pub fn levenshtein(a: &str, b: &str) -> usize {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    if a.is_empty() {
        return b.len();
    }
    if b.is_empty() {
        return a.len();
    }
    let mut prev: Vec<usize> = (0..=b.len()).collect();
    let mut curr = vec![0usize; b.len() + 1];
    for (i, ca) in a.iter().enumerate() {
        curr[0] = i + 1;
        for (j, cb) in b.iter().enumerate() {
            let cost = if ca == cb { 0 } else { 1 };
            curr[j + 1] = (prev[j + 1] + 1).min(curr[j] + 1).min(prev[j] + cost);
        }
        std::mem::swap(&mut prev, &mut curr);
    }
    prev[b.len()]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_case_ligatures_accents_and_punctuation() {
        assert_eq!(normalize("ﬁne"), "fine");
        assert_eq!(normalize("  The   STAFF!  "), "the staff");
        assert_eq!(normalize("Café"), "cafe");
        assert_eq!(normalize("Ainz Ooal Gown's"), "ainz ooal gown's");
    }

    #[test]
    fn canonical_strips_leading_articles() {
        assert_eq!(canonical("The Silver Key"), "silver key");
        assert_eq!(canonical("a staff"), "staff");
        assert_eq!(canonical("Keeper Sarn"), "keeper sarn");
        assert_eq!(canonical("THE HALL OF EMBERS"), "hall of embers");
    }

    #[test]
    fn tokenizes_and_strips_noise() {
        assert_eq!(
            tokenize("Take the Silver Key, now!"),
            vec!["take", "the", "silver", "key", "now"]
        );
    }

    #[test]
    fn levenshtein_counts_edits() {
        assert_eq!(levenshtein("key", "key"), 0);
        assert_eq!(levenshtein("key", "kay"), 1);
        assert_eq!(levenshtein("staff", "staf"), 1);
        assert_eq!(levenshtein("staff", "sword"), 4);
    }
}