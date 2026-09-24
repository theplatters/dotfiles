import QtQuick
import "../theme"

// Shared generated-markdown body (§6.1): assistant/generated markdown
// only. User-authored text stays Text.PlainText at the call site
// (untrusted-input rule); exact write previews stay PlainText in
// PreviewPanel so Confirm shows the exact bytes. Bounded lines +
// elide so bodies cannot grow unbounded inside card Flickables.
Text {
    id: root

    property int bodyMaxLines: 12

    color: Theme.text
    font.family: Theme.fontFamily
    textFormat: Text.MarkdownText
    wrapMode: Text.Wrap
    maximumLineCount: root.bodyMaxLines
    elide: Text.ElideRight
}
