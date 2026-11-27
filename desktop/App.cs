using System.Windows;

namespace SpikeMark.Desktop;

internal static class App
{
    [STAThread]
    public static void Main()
    {
        using var instance = new Mutex(true, @"Local\SpikeMark.Desktop", out bool firstInstance);
        if (!firstInstance)
        {
            MessageBox.Show("SpikeMark 已在运行。", "SpikeMark");
            return;
        }
        new Application().Run(new EditorWindow());
    }
}
