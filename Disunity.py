import sys
import os
import struct
import glob
import io
from uuid import UUID
from pprint import pprint
from munch import Munch

class ChunkedFileIO(io.BufferedIOBase):

    class Chunk():

        def __init__(self, path, pos):
            self.handle = open(path, "rb")
            self.handle.seek(0, io.SEEK_END)
            self.size = self.handle.tell()
            self.handle.seek(0, io.SEEK_SET)
            self.start = pos
            self.end = pos + self.size

        def tell(self):
            return self.start + self.handle.tell()

        def read(self, size=-1):
            return self.handle.read(size)

        def seek(self, offset):
            offset -= self.start
            if offset < 0 or offset > self.size:
                raise ValueError("Offset out of range:", offset)
            self.handle.seek(offset, io.SEEK_SET)

        def close(self):
            self.handle.close()

    chunks = []
    index = 0

    def __init__(self, paths):
        pos = 0
        for path in paths:
            chunk = self.Chunk(path, pos)
            # filter out empty chunks
            if chunk.size == 0:
                chunk.close()
                continue
            pos += chunk.size
            self.chunks.append(chunk)

    def chunk(self):
        return self.chunks[self.index]

    def chunk_next(self):
        if self.index + 1 >= len(self.chunks):
            return None

        self.index += 1
        return self.chunk()

    def chunk_find(self, pos):
        for self.index, chunk in enumerate(self.chunks):
            if chunk.end >= pos:
                break
        return chunk

    def read(self, size=-1):
        chunk = self.chunk()
        data = chunk.read(size)

        # get next chunk if required
        if not data:
            chunk = self.chunk_next()
            if chunk:
                data = chunk.read(size)

        return data

    def seek(self, offset, whence=io.SEEK_SET):
        chunk = self.chunk()

        # convert relative offset to absolute position
        pos = offset
        if whence == io.SEEK_CUR:
            pos += chunk.tell()
        elif whence == io.SEEK_END:
            pos += self.chunks[-1].end
        elif whence != io.SEEK_SET:
            raise NotImplementedError()

        # find new chunk if absolute position is outside current chunk
        if pos < chunk.start or pos > chunk.end:
            chunk = self.chunk_find(pos)

        chunk.seek(pos)

    def tell(self):
        return self.chunk().tell()

    def close(self):
        for chunk in self.chunks:
            chunk.close()
        super(ChunkedFileIO, self).close()

class BinaryReader:

    be = False

    def __init__(self, file):
        self.file = file

    def tell(self):
        return self.file.tell()

    def seek(self, offset, whence=0):
        self.file.seek(offset, whence)

    def align(self, pad):
        pos = self.tell()
        newpos = (pos + pad - 1) // pad * pad
        if newpos != pos:
            self.seek(newpos)

    def read(self, size):
        return self.file.read(size)

    def read_cstring(self):
        buf = bytearray()
        b = self.read_int8()
        while b and b != 0:
            buf.append(b)
            b = self.read_int8()

        return buf.decode("ascii")

    def read_struct(self, format):
        size = struct.calcsize(format)
        data = self.file.read(size)
        return struct.unpack(format, data)

    def read_int(self, type):
        if self.be:
            type = ">" + type
        return self.read_struct(type)[0]

    def read_uuid(self):
        data = self.read(16)
        return UUID(bytes=data)

    def read_int8(self):
        b = self.file.read(1)
        return b[0] if b else None

    def read_int16(self):
        return self.read_int("h")

    def read_uint16(self):
        return self.read_int("H")

    def read_int32(self):
        return self.read_int("i")

    def read_uint32(self):
        return self.read_int("I")

    def read_int64(self):
        return self.read_int("q")

    def read_uint64(self):
        return self.read_int("Q")

class SerializedFileReader:

    def read(self, file):
        r = BinaryReader(file)
        sf = Munch()
        self.read_header(r, sf)
        self.read_types(r, sf)
        self.read_objects(r, sf)
        if sf.header.version > 10:
            self.read_script_types(r, sf)
        self.read_externals(r, sf)

        return sf

    def read_header(self, r, sf):
        # the header always uses big-endian byte order
        r.be = True

        sf.header = Munch()
        sf.header.metadataSize = r.read_int32()
        sf.header.fileSize = r.read_int32()
        sf.header.version = r.read_int32()
        sf.header.dataOffset = r.read_int32()

        if sf.header.dataOffset > sf.header.fileSize:
            raise RuntimeError("Invalid dataOffset %d" % sf.header.dataOffset)

        if sf.header.metadataSize > sf.header.fileSize:
            raise RuntimeError("Invalid metadataSize %d" % sf.header.metadataSize)

        if sf.header.version >= 9:
            sf.header.endianness = r.read_int8()
            r.read(3) # reserved

        # newer formats use little-endian for the rest of the file
        if sf.header.version > 5:
            r.be = False

        # TODO: test more formats
        if sf.header.version != 15:
            raise NotImplementedError("Unsupported format version %d" % sf.header.version)

    def read_types(self, r, sf):
        sf.types = Munch()

        # older formats store the object data before the structure data
        if sf.header.version < 9:
            types_offset = sf.header.fileSize - sf.header.metadataSize + 1
            r.seek(types_offset)

        if sf.header.version > 6:
            sf.types.signature = r.read_cstring()
            sf.types.attributes = r.read_int32()

        if sf.header.version > 13:
            sf.types.embedded = r.read_int8() != 0

        sf.types.classes = {}

        num_classes = r.read_int32()
        for i in range(0, num_classes):
            bclass = Munch()

            class_id = r.read_int32()
            if class_id < 0:
                bclass.script_id = r.read_uuid()

            bclass.old_type_hash = r.read_uuid()

            if sf.types.embedded:
                # TODO
                raise NotImplementedError("Runtime type node reading")

            if class_id in sf.types.classes:
                raise RuntimeError("Duplicate class ID %d" % path_id)

            sf.types.classes[class_id] = bclass

    def read_objects(self, r, sf):
        sf.objects = {}

        num_entries = r.read_int32()

        for i in range(0, num_entries):
            if sf.header.version > 13:
                r.align(4)

            path_id = r.read_int64()

            obj = Munch()
            obj.byte_start = r.read_uint32()
            obj.byte_size = r.read_uint32()
            obj.type_id = r.read_int32()
            obj.class_id = r.read_int16()

            if sf.header.version > 13:
                obj.script_type_index = r.read_int16()
            else:
                obj.is_destroyed = r.read_int16() != 0

            if sf.header.version > 14:
                obj.stripped = r.read_int8() != 0

            if path_id in sf.objects:
                raise RuntimeError("Duplicate path ID %d" % path_id)

            sf.objects[path_id] = obj

    def read_script_types(self, r, sf):
        sf.script_types = []

        num_entries = r.read_int32()

        for i in range(0, num_entries):
            r.align(4)

            script_type = Munch()
            script_type.serialized_file_index = r.read_int32()
            script_type.identifier_in_file = r.read_int64()

            sf.script_types.append(script_type)

    def read_externals(self, r, sf):
        sf.externals = []

        num_entries = r.read_int32()
        for i in range(0, num_entries):
            external = Munch()

            if sf.header.version > 5:
                external.asset_path = r.read_cstring()

            external.guid = r.read_uuid()
            external.type = r.read_int32()
            external.file_path = r.read_cstring()

            sf.externals.append(external)

def main(argv):
    app = argv.pop(0)
    path = argv.pop(0)

    reader = SerializedFileReader()

    for globpath in glob.iglob(path):
        if os.path.isdir(globpath):
            continue

        fname, fext = os.path.splitext(globpath)
        if fext == ".resource":
            continue

        if fext == ".split0":
            index = 0
            splitpath = fname + fext
            splitpaths = []

            while os.path.exists(splitpath):
                splitpaths.append(splitpath)
                index += 1
                splitpath = fname + ".split%d" % index

            print(splitpaths[0])
            with ChunkedFileIO(splitpaths) as file:
                sf = reader.read(file)
                pprint(sf)
        elif fext[0:6] == ".split":
            continue
        else:
            print(globpath)
            with open(globpath, "rb") as file:
                sf = reader.read(file)
                pprint(sf)

    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))