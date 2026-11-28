using System.Windows;

namespace ShotDepth.Desktop;

internal static class App
{
    [STAThread]
    public static void Main()
    {
        using var instance = new Mutex(true, @"Local\SpikeMark.Desktop", out bool firstInstance);
        if (!firstInstance)
        {
            MessageBox.Show("ShotDepth 已在运行。", "ShotDepth");
            return;
        }
        new Application().Run(new EditorWindow());
    }
}
