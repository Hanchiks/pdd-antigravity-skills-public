import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.graphics.Point;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;
import java.io.File;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.concurrent.TimeoutException;

/** Read-only experiment. No signal from this prototype authorizes input. */
public final class ShortIdleDump {
    public static void main(String[] args) {
        Object wrapper = null;
        File output = null;
        int status = 0;
        try {
            if (args.length != 0) throw new IllegalArgumentException("no arguments expected");
            Class<?> wrapperType = Class.forName("com.android.uiautomator.core.UiAutomationShellWrapper");
            wrapper = wrapperType.getConstructor().newInstance();
            wrapperType.getMethod("connect").invoke(wrapper);
            UiAutomation ui = (UiAutomation) wrapperType.getMethod("getUiAutomation").invoke(wrapper);
            AccessibilityServiceInfo info = ui.getServiceInfo();
            int flags = AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
                      | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
            info.flags |= flags;
            ui.setServiceInfo(info);
            if ((ui.getServiceInfo().flags & flags) != flags)
                throw new IllegalStateException("required accessibility flags unavailable");
            // Unlike the system's 1000/10000ms wait, this experiment uses a
            // shorter interval. A timeout fails closed instead of dumping.
            ui.waitForIdle(100, 3000);
            AccessibilityNodeInfo root = ui.getRootInActiveWindow();
            if (root == null || !"com.xunmeng.pinduoduo".contentEquals(root.getPackageName()))
                throw new IllegalStateException("active Pinduoduo root unavailable");
            Class<?> displayManager = Class.forName("android.hardware.display.DisplayManagerGlobal");
            Object manager = displayManager.getMethod("getInstance").invoke(null);
            Display display = (Display) displayManager.getMethod("getRealDisplay", int.class).invoke(manager, 0);
            if (display == null) throw new IllegalStateException("display unavailable");
            Point size = new Point();
            display.getSize(size);
            int rotation = display.getRotation();
            if (size.x <= 0 || size.y <= 0) throw new IllegalStateException("invalid display size");
            output = File.createTempFile("pdd_short_idle_", ".xml", new File("/data/local/tmp"));
            Class<?> dumper = Class.forName("com.android.uiautomator.core.AccessibilityNodeInfoDumper");
            Method dump = dumper.getMethod("dumpWindowToFile", AccessibilityNodeInfo.class,
                    File.class, int.class, int.class, int.class);
            dump.invoke(null, root, output, rotation, size.x, size.y);
            if (!output.isFile() || output.length() == 0 || output.length() > 20000000L)
                throw new IllegalStateException("empty or oversized dump");
            String xml = new String(Files.readAllBytes(output.toPath()), StandardCharsets.UTF_8);
            if (!xml.startsWith("<?xml") || !xml.endsWith("</hierarchy>"))
                throw new IllegalStateException("invalid or incomplete dump");
            System.out.print(xml);
        } catch (TimeoutException e) {
            System.err.println("SHORT_IDLE_TIMEOUT");
            status = 2;
        } catch (Throwable e) {
            System.err.println("SHORT_IDLE_DUMP_FAILED: " + e.getClass().getSimpleName());
            status = 2;
        } finally {
            if (output != null && output.exists()) output.delete();
            if (wrapper != null) {
                try { wrapper.getClass().getMethod("disconnect").invoke(wrapper); }
                catch (Throwable ignored) { }
            }
        }
        if (status != 0) System.exit(status);
    }
}
