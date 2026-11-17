// Techne: a deliberately small SolidWorks add-in for the Techne MCP tools.
//
// What it does: it journals what a person changes by hand in SolidWorks (features
// added, deleted, renamed, dimensions changed, rebuilds, saves) so the AI's
// watch_changes tool can see it, one JSON line per event, in
//   %LOCALAPPDATA%\Techne\journal\<document title>.jsonl
//
// What it does not do: no UI, no task pane, no timers, no background threads and
// no work at idle. It only listens to the active document's events and appends a
// line when one fires, so it costs SolidWorks nothing measurable.
//
// Written for the C# 5 compiler that ships with .NET Framework 4.x (csc.exe), so
// it builds on any Windows machine without Visual Studio; install.ps1 builds it.

using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using Microsoft.Win32;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SolidWorks.Interop.swpublished;

namespace Techne
{
    [ComVisible(true)]
    [Guid("6E3C2A51-9B7D-4F3E-A8C1-5D2B9E7F4A10")]
    [ProgId("Techne.Journal")]
    public class TechneAddin : ISwAddin
    {
        private const string AddinTitle = "Techne";
        private const string AddinDescription = "Journals manual edits for the Techne SolidWorks MCP tools.";

        private SldWorks app;
        private ModelDoc2 model;
        private PartDoc part;
        private AssemblyDoc assembly;
        private string journalDir;

        // ----------------------------------------------------------------- lifetime

        public bool ConnectToSW(object ThisSW, int Cookie)
        {
            app = (SldWorks)ThisSW;
            app.SetAddinCallbackInfo2(0, this, Cookie);
            journalDir = Path.Combine(System.Environment.GetFolderPath(System.Environment.SpecialFolder.LocalApplicationData),
                                      "Techne", "journal");
            Directory.CreateDirectory(journalDir);
            app.ActiveModelDocChangeNotify += OnActiveDocChange;
            Attach(app.ActiveDoc as ModelDoc2);
            return true;
        }

        public bool DisconnectFromSW()
        {
            Detach();
            if (app != null)
            {
                app.ActiveModelDocChangeNotify -= OnActiveDocChange;
            }
            app = null;
            GC.Collect();
            GC.WaitForPendingFinalizers();
            return true;
        }

        private int OnActiveDocChange()
        {
            Detach();
            Attach(app.ActiveDoc as ModelDoc2);
            return 0;
        }

        // ----------------------------------------------------------------- documents

        private void Attach(ModelDoc2 doc)
        {
            if (doc == null)
            {
                return;
            }
            model = doc;
            int type = doc.GetType();
            if (type == (int)swDocumentTypes_e.swDocPART)
            {
                part = (PartDoc)doc;
                part.AddItemNotify += OnPartAddItem;
                part.DeleteItemNotify += OnPartDeleteItem;
                part.RenameItemNotify += OnPartRenameItem;
                part.DimensionChangeNotify += OnPartDimensionChange;
                part.RegenPostNotify2 += OnPartRegenPost;
                part.FileSaveNotify += OnPartSave;
            }
            else if (type == (int)swDocumentTypes_e.swDocASSEMBLY)
            {
                assembly = (AssemblyDoc)doc;
                assembly.AddItemNotify += OnAsmAddItem;
                assembly.DeleteItemNotify += OnAsmDeleteItem;
                assembly.RenameItemNotify += OnAsmRenameItem;
                assembly.FileSaveNotify += OnAsmSave;
            }
        }

        private void Detach()
        {
            if (part != null)
            {
                part.AddItemNotify -= OnPartAddItem;
                part.DeleteItemNotify -= OnPartDeleteItem;
                part.RenameItemNotify -= OnPartRenameItem;
                part.DimensionChangeNotify -= OnPartDimensionChange;
                part.RegenPostNotify2 -= OnPartRegenPost;
                part.FileSaveNotify -= OnPartSave;
                part = null;
            }
            if (assembly != null)
            {
                assembly.AddItemNotify -= OnAsmAddItem;
                assembly.DeleteItemNotify -= OnAsmDeleteItem;
                assembly.RenameItemNotify -= OnAsmRenameItem;
                assembly.FileSaveNotify -= OnAsmSave;
                assembly = null;
            }
            model = null;
        }

        // ----------------------------------------------------------------- part events

        private int OnPartAddItem(int entityType, string itemName) { return Log("added", itemName, entityType, null); }
        private int OnPartDeleteItem(int entityType, string itemName) { return Log("deleted", itemName, entityType, null); }
        private int OnPartRenameItem(int entityType, string oldName, string newName) { return Log("renamed", oldName, entityType, newName); }
        private int OnPartRegenPost(object stopFeature) { return Log("rebuilt", null, -1, null); }
        private int OnPartSave(string fileName) { return Log("saved", fileName, -1, null); }

        private int OnPartDimensionChange(object displayDim)
        {
            try
            {
                DisplayDimension disp = displayDim as DisplayDimension;
                Dimension dim = disp == null ? null : disp.GetDimension2(0);
                if (dim != null)
                {
                    // millimetres, the unit the AI works in (the API gives metres)
                    double mm = dim.SystemValue * 1000.0;
                    return Log("dimension", dim.FullName, -1, mm.ToString("R", System.Globalization.CultureInfo.InvariantCulture));
                }
            }
            catch (Exception)
            {
            }
            return Log("dimension", null, -1, null);
        }

        // ----------------------------------------------------------------- assembly events

        private int OnAsmAddItem(int entityType, string itemName) { return Log("added", itemName, entityType, null); }
        private int OnAsmDeleteItem(int entityType, string itemName) { return Log("deleted", itemName, entityType, null); }
        private int OnAsmRenameItem(int entityType, string oldName, string newName) { return Log("renamed", oldName, entityType, newName); }
        private int OnAsmSave(string fileName) { return Log("saved", fileName, -1, null); }

        // ----------------------------------------------------------------- journal

        private int Log(string evt, string name, int entityType, string detail)
        {
            try
            {
                if (model == null)
                {
                    return 0;
                }
                string title = model.GetTitle() ?? "untitled";
                string file = Path.Combine(journalDir, Regex.Replace(title, @"[^\w.-]+", "_") + ".jsonl");
                StringBuilder line = new StringBuilder(160);
                line.Append("{\"t\":\"").Append(DateTime.Now.ToString("yyyy-MM-ddTHH:mm:ss")).Append('"');
                line.Append(",\"event\":\"").Append(evt).Append('"');
                if (name != null)
                {
                    line.Append(",\"name\":\"").Append(Escape(name)).Append('"');
                }
                if (entityType >= 0)
                {
                    line.Append(",\"entity_type\":").Append(entityType);
                }
                if (detail != null)
                {
                    line.Append(",\"detail\":\"").Append(Escape(detail)).Append('"');
                }
                line.Append("}\n");
                File.AppendAllText(file, line.ToString(), Encoding.UTF8);
            }
            catch (Exception)
            {
                // a journal problem must never disturb SolidWorks
            }
            return 0;
        }

        private static string Escape(string s)
        {
            StringBuilder sb = new StringBuilder(s.Length + 8);
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < ' ')
                        {
                            sb.AppendFormat("\\u{0:x4}", (int)c);
                        }
                        else
                        {
                            sb.Append(c);
                        }
                        break;
                }
            }
            return sb.ToString();
        }

        // ----------------------------------------------------------------- COM registration

        [ComRegisterFunction]
        public static void RegisterFunction(Type t)
        {
            string key = "SOFTWARE\\SolidWorks\\Addins\\{" + t.GUID.ToString() + "}";
            using (RegistryKey addin = Registry.LocalMachine.CreateSubKey(key))
            {
                addin.SetValue(null, 1);
                addin.SetValue("Description", AddinDescription);
                addin.SetValue("Title", AddinTitle);
            }
            using (RegistryKey startup = Registry.CurrentUser.CreateSubKey(
                "Software\\SolidWorks\\AddInsStartup\\{" + t.GUID.ToString() + "}"))
            {
                startup.SetValue(null, 1, RegistryValueKind.DWord);
            }
        }

        [ComUnregisterFunction]
        public static void UnregisterFunction(Type t)
        {
            try
            {
                Registry.LocalMachine.DeleteSubKey("SOFTWARE\\SolidWorks\\Addins\\{" + t.GUID.ToString() + "}", false);
                Registry.CurrentUser.DeleteSubKey("Software\\SolidWorks\\AddInsStartup\\{" + t.GUID.ToString() + "}", false);
            }
            catch (Exception)
            {
            }
        }
    }
}
