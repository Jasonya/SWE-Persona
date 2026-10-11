"""Public-only OpenCC baseline observation, executed inside its pinned image."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


def main():
    root = Path("/testbed")
    build = subprocess.run(["cmake", "--build", "build", "--clean-first", "-j2"], cwd=root,
                           capture_output=True, text=True, timeout=600)
    if build.returncode:
        raise RuntimeError("Baseline source build failed: " + (build.stdout + build.stderr)[-2000:])
    source_paths = ("data/config/tw2sp.json", "src/Conversion.cpp", "src/ConversionChain.cpp",
                    "src/Converter.cpp", "src/PrefixMatch.cpp")
    sources = []
    for path in source_paths:
        data = (root / path).read_bytes()
        sources.append({"path": path, "sha256": hashlib.sha256(data).hexdigest(),
                        "text": data.decode("utf-8")})
    # Public issue rows, not held-out tests or the upstream reference patch.
    cases = [("\u80fa\u57fa\u9178", "\u6c28\u57fa\u9178"),
             ("\u7518\u80fa\u9178", "\u7518\u6c28\u9178")]
    observations, builds = [], []
    # The shipped build/data can contain newer compiled dictionaries than the
    # checked-out source. Rebuild every dictionary used by this configuration.
    with tempfile.TemporaryDirectory(prefix="persona-opencc-baseline-") as temporary:
        dictionaries = ("TSPhrases", "TWPhrasesRev", "TWVariantsRevPhrases", "TWVariantsRev", "TSCharactersExt", "TSCharacters")
        config = json.loads((root / "data/config/tw2sp.json").read_text())
        def absolute_files(value):
            if isinstance(value, dict):
                if value.get("type") == "ocd2":
                    value["file"] = str(Path(temporary) / value["file"])
                for child in value.values():
                    absolute_files(child)
            elif isinstance(value, list):
                for child in value:
                    absolute_files(child)
        absolute_files(config)
        config_path = Path(temporary) / "tw2sp.json"
        config_path.write_text(json.dumps(config))
        generated = {"TWVariantsRev": ("reverse.py", "TWVariants"),
                     "TSCharactersExt": ("extract_tofu_risk.py", "TSCharacters")}
        for name in dictionaries:
            source = root / "data/dictionary" / (name + ".txt")
            if name in generated:
                script, original = generated[name]
                source = Path(temporary) / (name + ".txt")
                generation = ["python3", str(root / "data/scripts" / script),
                              str(root / "data/dictionary" / (original + ".txt")), str(source)]
                result = subprocess.run(generation, cwd=root, capture_output=True, encoding="utf-8", timeout=60)
                builds.append({"command": generation, "exit_code": result.returncode,
                               "stdout": result.stdout, "stderr": result.stderr})
                if result.returncode:
                    raise RuntimeError("Baseline dictionary generation failed: " + name)
            command = ["build/src/tools/opencc_dict", "-i", str(source), "-o", str(Path(temporary) / (name + ".ocd2")), "-f", "text", "-t", "ocd2"]
            result = subprocess.run(command, cwd=root, capture_output=True, encoding="utf-8", timeout=60)
            builds.append({"command": command, "exit_code": result.returncode,
                           "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                           "stdout": result.stdout, "stderr": result.stderr})
            if result.returncode:
                raise RuntimeError("Baseline dictionary rebuild failed: " + name)
        for value, expected in cases:
            command = ["build/src/tools/opencc", "-c", str(config_path)]
            result = subprocess.run(command, input=value + "\n", cwd=root, capture_output=True,
                                    encoding="utf-8", timeout=30)
            inspect_result = subprocess.run(command + ["--inspect"], input=value + "\n", cwd=root,
                                            capture_output=True, encoding="utf-8", timeout=30)
            observations.append({"command": command, "input": value + "\n", "expected": expected + "\n",
                                 "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                                 "inspection": {"command": command + ["--inspect"],
                                                "exit_code": inspect_result.returncode,
                                                "stdout": inspect_result.stdout, "stderr": inspect_result.stderr}})
    dictionary = root / "data/dictionary/TWPhrasesRev.txt"
    data = dictionary.read_bytes()
    entries = {line.split("\t")[0]: line for line in data.decode().splitlines()
               if line and not line.startswith("#")}
    print(json.dumps({"base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                      "sources": sources, "observations": observations, "dictionary_rebuild": builds,
                      "source_build": {"exit_code": build.returncode, "output_tail": (build.stdout + build.stderr)[-1000:]},
                      "dictionary": {"path": str(dictionary.relative_to(root)),
                                     "sha256": hashlib.sha256(data).hexdigest(),
                                     "requested_entries": {value: entries.get(value) for value, _ in cases}}},
                     ensure_ascii=True))


if __name__ == "__main__":
    main()
