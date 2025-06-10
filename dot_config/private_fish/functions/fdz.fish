function fdz
    set file (fd . $argv | fzf --preview "bat --style=numbers --color=always {} || cat {}" --bind "ctrl-f:reload:rg --files-with-matches --no-messages {q} || true" --phony --query "")
    if test -d $file
        cd $file
    else
        cd (dirname $file)
    end
end
