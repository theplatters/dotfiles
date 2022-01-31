

cat $1 | rofi -dmenu -i -p $2 | xargs -n 1 $3 
