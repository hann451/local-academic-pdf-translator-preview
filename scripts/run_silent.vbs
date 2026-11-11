Option Explicit

Dim fso, shell, scriptDir, launcherPs1, psExe, argsBuilder, i, arg, cmdLine, dq
dq = Chr(34)
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
launcherPs1 = fso.BuildPath(scriptDir, "launcher.ps1")

If Not fso.FileExists(launcherPs1) Then
    MsgBox (ChrW(&H8D77) & ChrW(&H52D5) & ChrW(&H30B9) & ChrW(&H30AF) & ChrW(&H30EA) & ChrW(&H30D7) & ChrW(&H30C8) & ChrW(&H304C) & ChrW(&H898B) & ChrW(&H3064) & ChrW(&H304B) & ChrW(&H308A) & ChrW(&H307E) & ChrW(&H305B) & ChrW(&H3093) & ChrW(&H003A)) & vbCrLf & launcherPs1, vbCritical, (ChrW(&H0050) & ChrW(&H0061) & ChrW(&H0070) & ChrW(&H0065) & ChrW(&H0072) & ChrW(&H0042) & ChrW(&H0072) & ChrW(&H0069) & ChrW(&H0064) & ChrW(&H0067) & ChrW(&H0065) & ChrW(&H0020) & ChrW(&H004C) & ChrW(&H006F) & ChrW(&H0063) & ChrW(&H0061) & ChrW(&H006C) & ChrW(&H0020) & ChrW(&H8D77) & ChrW(&H52D5) & ChrW(&H30A8) & ChrW(&H30E9) & ChrW(&H30FC))
    WScript.Quit 1
End If

psExe = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
If Not fso.FileExists(psExe) Then
    psExe = "powershell.exe"
End If

argsBuilder = ""
For i = 0 To WScript.Arguments.Count - 1
    arg = WScript.Arguments(i)
    If InStr(arg, " ") > 0 Or InStr(arg, dq) > 0 Then
        arg = Replace(arg, dq, "\" & dq)
        argsBuilder = argsBuilder & " " & dq & arg & dq
    Else
        argsBuilder = argsBuilder & " " & arg
    End If
Next

cmdLine = dq & psExe & dq & " -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & dq & launcherPs1 & dq & argsBuilder
shell.Run cmdLine, 0, False
WScript.Quit 0
