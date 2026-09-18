Set shell = CreateObject("WScript.Shell")

exitCode = shell.Run("""C:\Users\andolabuser\ai_notifier\run_tasks_listener.cmd""", 0, True)
WScript.Quit exitCode
