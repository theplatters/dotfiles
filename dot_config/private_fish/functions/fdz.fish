function fdz
    set file (fd . $argv | fzf \
        --preview "bat --style=numbers --color=always {} || cat {}" \
        --bind "change:reload:(fd . $argv; rg --files-with-matches --no-messages {q}) | sort -u || true" \
        --query "" \
        --phony \
        --preview-window=right:60%)

    if test -d $file
        cd $file
    else
        cd (dirname $file)
    end
end
