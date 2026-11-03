"""Registration bytecode, source scanning, and a real released-asset regression."""
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bridge_contract", ROOT / ".github/actions/native-bridge-check/bridge_contract.py")
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class CallSiteTests(unittest.TestCase):
    def names(self, source):
        return [names for _, names in bridge.lua_calls(source, ("sdk",))]

    def test_calls_capability_checks_and_literal_indexing(self):
        self.assertEqual(self.names('type(sdk.restorePurchases) == "function"; sdk.purchase(); sdk["completePurchase"]()'),
                         [{"restorePurchases"}, {"purchase"}, {"completePurchase"}])

    def test_comments_and_strings_are_not_calls(self):
        self.assertEqual(self.names('-- sdk.missing()\nlocal x = "sdk.missing"\n--[[sdk.missing()]]\nsdk.purchase()'), [{"purchase"}])

    def test_capability_conjunction_is_not_a_fallback(self):
        self.assertEqual(self.names('return type(sdk.purchase) == "function" and type(sdk.restorePurchases) == "function"'),
                         [{"purchase"}, {"restorePurchases"}])

    def test_first_available_return_selector_is_one_contract(self):
        source = '''if type(sdk.restorePurchases) == "function" then return sdk.restorePurchases end
                    if type(sdk.restorePurchasesJson) == "function" then return sdk.restorePurchasesJson end'''
        self.assertEqual(self.names(source), [{"restorePurchases", "restorePurchasesJson"}])

    def test_dynamic_lua_dispatch_fails(self):
        with self.assertRaises(bridge.ContractError):
            self.names('sdk[method]()')

    def test_renamed_import_and_alias_remain_checked(self):
        self.assertEqual(self.names('local bridge = require("plugin.cubeage"); local copy = bridge; copy.missing()'),
                         [{"missing"}])

    def test_java_dispatch_checks_owner_not_global_name_union(self):
        class Native:
            parent = None
            methods = [(1, "purchase", "()V", {})]
        classes = {"com/cubeage/sdk/Other": Native()}
        calls = bridge.unity_calls('const string SdkClass = "com.cubeage.sdk.CubeageSDK"; '
                                   'var sdk = new AndroidJavaClass(SdkClass); sdk.CallStatic("purchase");', classes)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][3])

    def test_dynamic_java_dispatch_fails(self):
        with self.assertRaises(bridge.ContractError):
            bridge.unity_calls('var sdk = new AndroidJavaClass("com.cubeage.sdk.CubeageSDK"); sdk.CallStatic(method);', {})


@unittest.skipUnless(shutil.which("javac"), "requires javac to compile registration bytecode")
class BytecodeTests(unittest.TestCase):
    def test_only_instantiated_registered_functions_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = {
                "com/naef/jnlua/NamedJavaFunction.java": 'package com.naef.jnlua; public interface NamedJavaFunction {String getName();}',
                "com/naef/jnlua/LuaState.java": 'package com.naef.jnlua; public class LuaState {public void setField(int i, String n) {}}',
                "plugin/cubeage/LuaLoader.java": '''package plugin.cubeage;
                  import com.naef.jnlua.*;
                  public class LuaLoader {
                    static class Registered implements NamedJavaFunction { public String getName() {return "purchase";} }
                    static class Orphan implements NamedJavaFunction { public String getName() {return "restorePurchases";} }
                    public int invoke(LuaState state) {
                      NamedJavaFunction[] names = {new Registered()};
                      for (NamedJavaFunction name : names) state.setField(-2, name.getName());
                      return 1;
                    }
                  }''',
            }
            for name, content in sources.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            subprocess.run(["javac", *map(str, root.rglob("*.java"))], check=True, capture_output=True)
            jar = io.BytesIO()
            with zipfile.ZipFile(jar, "w") as archive:
                for path in root.rglob("*.class"):
                    archive.write(path, path.relative_to(root))
            aar = io.BytesIO()
            with zipfile.ZipFile(aar, "w") as archive:
                archive.writestr("classes.jar", jar.getvalue())
            self.assertEqual(bridge.lua_exports(bridge.aar_classes(aar.getvalue())), {"purchase"})


class ReleasedArtifactTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("NATIVE_BRIDGE_RELEASE_ARTIFACT"), "set published android.tgz for real artifact acceptance")
    def test_witnessed_restore_regression_against_published_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = root / "client/src"
            client.mkdir(parents=True)
            (client / "build.settings").write_text('url="https://github.com/Cubeage/cubeage-native-sdk/releases/download/v4.3.0/android.tgz"')
            path = client / "purchase.lua"
            path.write_text('return type(sdk.restorePurchases) == "function" and type(sdk.querySubscriptions) == "function"')
            before = bridge.check(root, os.environ["NATIVE_BRIDGE_RELEASE_ARTIFACT"])
            self.assertEqual({c["names"][0] for c in before["missing"]}, {"restorePurchases", "querySubscriptions"})
            path.write_text('return type(sdk.restorePurchasesJson) == "function" and type(sdk.querySubscriptionsJson) == "function"')
            self.assertEqual(bridge.check(root, os.environ["NATIVE_BRIDGE_RELEASE_ARTIFACT"])["missing"], [])


if __name__ == "__main__":
    unittest.main()
