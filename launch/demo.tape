# Records docs/demo.gif with VHS (https://github.com/charmbracelet/vhs):
#   vhs launch/demo.tape
# The demo is offline and simulated: no keys, and the result is the same every time.
Output docs/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1280
Set Height 860
Set Padding 28
Set Theme "GitHub Dark"
Set TypingSpeed 45ms
Env FORCE_COLOR "1"

Hide
Type "export PATH=$PWD/scripts:$PATH && clear"
Enter
Show

Sleep 600ms
Type "crosscheck demo"
Sleep 400ms
Enter
Sleep 9s
