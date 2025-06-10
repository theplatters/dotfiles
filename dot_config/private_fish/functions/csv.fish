function csv
    set -l sep ","
    if test (count $argv) -eq 0
        echo "Usage: csv <file> [separator]"
        return 1
    end

    set -l file $argv[1]
    if test (count $argv) -ge 2
        set sep $argv[2]
    end

    column -s"$sep" -t <"$file" | less -S
end
