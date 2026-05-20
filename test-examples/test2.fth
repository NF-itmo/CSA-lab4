1 constant flag

flag @ if            \ ветвление: if/else/then
    1
else
    2 3 out
then
    3 do             \ цикл: do/loop (предикат берётся со стека, 0 завершает цикл)
        1 3 out
        1 3 out
        1 3 out
        3 3 out
        1-
    loop

bye