"""Linear-time collection of large terminal output, without truncating test logs."""
import codecs
import queue
import time


def make_output_reader(metadata_type, ansi_escape):
    def read_raw_output(self, timeout=30):
        chunks = []
        decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        # PS1 metadata is a small control record. Keep a bounded search window;
        # retain ALL output separately for the official parser and saved log.
        tail = ""
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            try:
                chunk = self.output_queue.get(timeout=0.1)
            except queue.Empty:
                # EOF/container exit cannot produce a future prompt. Waiting
                # the entire command budget here only hides a broken runtime.
                thread = getattr(self, "output_thread", None)
                if thread is not None and not thread.is_alive():
                    break
                continue
            decoded = decoder.decode(chunk)
            chunks.append(decoded)
            tail = (tail + decoded)[-65536:]
            if "###PS1END###" not in tail:
                continue
            clean_tail = ansi_escape.sub("", tail).replace("\r", "")
            if metadata_type.matches_ps1_metadata(clean_tail):
                break
        chunks.append(decoder.decode(b"", final=True))
        output = ansi_escape.sub("", "".join(chunks)).replace("\r", "")
        matches = metadata_type.matches_ps1_metadata(output)
        metadata = metadata_type.from_ps1_match(matches[-1]) if matches else None
        return self._combine_outputs_between_matches(output, matches), metadata

    return read_raw_output
