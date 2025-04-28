function no
    set file (fd $argv | fzf --preview "cat {}")
    if test -n "$file"
        if test -d "$file"
            cd "$file"
            nvim
        else
            cd (dirname "$file")
            nvim (basename "$file")
        end
    end
end
