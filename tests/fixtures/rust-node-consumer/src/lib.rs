#[cfg(test)]
mod tests {
    #[test]
    fn rust_can_import_the_locked_node_dependency() {
        let result = std::process::Command::new("node")
            .args(["--input-type=module", "-e", "import value from 'ci-fixture'; if (value !== 7 || Number(process.versions.node.split('.')[0]) < 22) process.exit(1);"])
            .current_dir(env!("CARGO_MANIFEST_DIR"))
            .output()
            .expect("the declared Node runtime must be installed");
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
    }
}
