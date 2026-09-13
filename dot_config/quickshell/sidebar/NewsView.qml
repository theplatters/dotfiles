import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../theme"

ColumnLayout {
    id: root
    spacing: 10
    
    ListModel {
        id: newsModel
    }

    property string statusMessage: ""

    Component.onCompleted: {
        fetchNews();
    }

    Text {
        text: "Global News"
        font.pixelSize: 20
        font.bold: true
        color: Theme.text
        Layout.margins: 10
    }

    Text {
        visible: statusMessage !== ""
        text: statusMessage
        color: Theme.red
        font.pixelSize: 12
        Layout.margins: 10
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
    }

    ListView {
        id: newsList
        Layout.fillWidth: true
        Layout.fillHeight: true
        model: newsModel
        clip: true
        spacing: 15
        
        delegate: ItemDelegate {
            width: newsList.width - 20
            x: 10
            
            contentItem: ColumnLayout {
                spacing: 5
                Text {
                    Layout.fillWidth: true
                    text: model.title
                    color: Theme.mauve
                    font.pixelSize: 16
                    font.bold: true
                    wrapMode: Text.WordWrap
                }
                Text {
                    Layout.fillWidth: true
                    text: model.pubDate
                    color: Theme.text
                    font.pixelSize: 12
                    opacity: 0.7
                }
            }
            
            background: Rectangle {
                color: hovered ? Theme.surface0 : Theme.mantle
                radius: 10
            }
            
            onClicked: Qt.openUrlExternally(model.link)
        }
    }

    function fetchNews() {
        statusMessage = "Fetching news...";
        let xhr = new XMLHttpRequest();
        xhr.open("GET", "https://feeds.bbci.co.uk/news/world/rss.xml");
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE) {
                if (xhr.status === 200) {
                    statusMessage = "";
                    parseRSS(xhr.responseText);
                } else {
                    statusMessage = "Error: " + xhr.status + " " + (xhr.status === 429 ? "Too Many Requests" : "Fetch Failed");
                }
            }
        };
        xhr.send();
    }

    function parseRSS(xmlText) {
        console.log("Parsing XML response (length:", xmlText.length, ")");
        newsModel.clear();
        // A very simple regex-based RSS parser since we don't have a full DOM parser
        let itemRegex = /<item>([\s\S]*?)<\/item>/g;
        let match;
        let count = 0;
        while ((match = itemRegex.exec(xmlText)) !== null) {
            let itemContent = match[1];
            let title = extractTag(itemContent, "title");
            let link = extractTag(itemContent, "link");
            let pubDate = extractTag(itemContent, "pubDate");
            
            newsModel.append({
                title: title,
                link: link,
                pubDate: pubDate
            });
            count++;
        }
        console.log("Parsed", count, "items.");
    }

    function extractTag(content, tag) {
        let regex = new RegExp("<" + tag + "[^>]*>([\\s\\S]*?)<\\/" + tag + ">");
        let match = regex.exec(content);
        if (match && match[1]) {
            let val = match[1].trim();
            // Basic unescape
            val = val.replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1");
            return val;
        }
        return "";
    }
}
