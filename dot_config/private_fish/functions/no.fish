function no
    set file (fd $argv | fzf --preview "cat {}")
    if test -n $file
        cd (dirname $file)
        nvim (basename $file)
    end
end
