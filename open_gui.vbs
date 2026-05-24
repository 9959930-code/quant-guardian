Option Explicit

Dim shell, fso, root, outputDir, logFile, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
outputDir = fso.BuildPath(root, "output")
If Not fso.FolderExists(outputDir) Then
  fso.CreateFolder(outputDir)
End If

logFile = fso.BuildPath(outputDir, "open_gui.log")
cmd = "%ComSpec% /c cd /d " & Chr(34) & root & Chr(34) & _
      " && python launch_dashboard.py --refresh > " & Chr(34) & logFile & Chr(34) & " 2>&1"

shell.Run cmd, 0, True
