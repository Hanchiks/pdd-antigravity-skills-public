import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.graphics.Rect;
import android.util.Xml;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.io.StringWriter;
import java.util.List;
import org.xmlpull.v1.XmlSerializer;

// Exploratory prototype only. No device compatibility or safety guarantees.
public final class NoIdleDump {
    private static int count;
    private static String text(CharSequence s) { return s == null ? "" : s.toString(); }
    private static void attr(XmlSerializer x, String key, Object val) throws Exception {
        x.attribute(null, key, val == null ? "" : val.toString());
    }
    private static void node(XmlSerializer x, AccessibilityNodeInfo n, int depth) throws Exception {
        if (n == null || depth > 120 || ++count > 50000) throw new IllegalStateException("missing node or limit");
        Rect r = new Rect(); n.getBoundsInScreen(r);
        x.startTag(null, "node");
        attr(x, "text", text(n.getText()));
        attr(x, "content-desc", text(n.getContentDescription()));
        attr(x, "resource-id", n.getViewIdResourceName());
        attr(x, "class", text(n.getClassName()));
        attr(x, "package", text(n.getPackageName()));
        attr(x, "bounds", "["+r.left+","+r.top+"]["+r.right+","+r.bottom+"]");
        attr(x, "clickable", n.isClickable());
        attr(x, "enabled", n.isEnabled());
        attr(x, "visible-to-user", n.isVisibleToUser());
        attr(x, "focused", n.isFocused());
        attr(x, "editable", n.isEditable());
        for (int i = 0; i < n.getChildCount(); i++) node(x, n.getChild(i), depth + 1);
        x.endTag(null, "node");
    }
    public static void main(String[] args) throws Exception {
        Object wrapper = null;
        try {
            Class<?> cls = Class.forName("com.android.uiautomator.core.UiAutomationShellWrapper");
            wrapper = cls.getConstructor().newInstance();
            cls.getMethod("connect").invoke(wrapper);
            UiAutomation ui = (UiAutomation) cls.getMethod("getUiAutomation").invoke(wrapper);
            AccessibilityServiceInfo info = ui.getServiceInfo();
            int flags = AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
                      | AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                      | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
            info.flags |= flags;
            ui.setServiceInfo(info);
            if ((ui.getServiceInfo().flags & flags) != flags) throw new IllegalStateException("required flags unavailable");
            List<AccessibilityWindowInfo> windows = null;
            for (int retry = 0; retry < 5; retry++) {
                windows = ui.getWindows();
                if (windows != null && !windows.isEmpty()) break;
                Thread.sleep(100);
            }
            if (windows == null || windows.isEmpty()) throw new IllegalStateException("no windows");
            StringWriter s = new StringWriter();
            XmlSerializer x = Xml.newSerializer();
            x.setOutput(s); x.startDocument("UTF-8", true); x.startTag(null, "hierarchy");
            for (AccessibilityWindowInfo window : windows) {
                AccessibilityNodeInfo root = window.getRoot();
                if (root == null) throw new IllegalStateException("window root missing");
                node(x, root, 0);
            }
            x.endTag(null, "hierarchy"); x.endDocument();
            System.out.print(s.toString());
        } finally {
            if (wrapper != null) wrapper.getClass().getMethod("disconnect").invoke(wrapper);
        }
    }
}
