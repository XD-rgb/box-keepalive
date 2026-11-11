' box-keepalive.vbs —— 登录时静默拉起保活循环，不弹窗
' 放在 shell:startup 里即可自启；手动测试：wscript box-keepalive.vbs
Dim sh, pyw, script, root
Set sh = CreateObject("WScript.Shell")
root = "C:\Users\Administrator\WorkBuddy\2026-11-11-09-52-10\box-keepalive"
pyw = "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
script = root & "\keepalive.py"

If Not CreateObject("Scripting.FileSystemObject").FileExists(pyw) Then
  pyw = "pythonw.exe"
End If

sh.CurrentDirectory = root
sh.Run """" & pyw & """ """ & script & """ --loop", 0, False
