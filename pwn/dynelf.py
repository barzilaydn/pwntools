import pwn

def sysv_hash(symbol):
    h = 0
    g = 0
    for c in symbol:
        h = (h << 4) + ord(c)
        g = h & 0xf0000000
        h ^= (g >> 24)
        h &= ~g
    return h & 0xffffffff

def gnu_hash(s):
    h = 0
    h = 5381
    for c in s:
        h = h * 33 + ord(c)
    return h & 0xffffffff

class DynELF:
    def __init__(self, path, leak, base = None, PIE = False):
        if isinstance(path, pwn.ELF):
            self.elf = path
        else:
            self.elf = pwn.elf.load(path)
        self.leak = leak
        self.base = base
        self.PIE = PIE

    def lookup (self, symb, lib = 'libc'):
        if self.elf.elfclass == 'ELF32':
            return self._lookup32(symb, lib)
        if self.elf.elfclass == 'ELF64':
            return self._lookup64(symb, lib)

    def _lookup32 (self, symb, lib):
        base = self.base
        leak = self.leak
        gotoff = self.elf.sections['.got.plt']['addr']

        def b(addr):
            return leak.b(addr)
        def d(addr):
            return leak.d(addr)
        def s(addr):
            return leak.s(addr)

        pwn.log.waitfor('Resolving "%s"' % symb)
        def status(s):
            pwn.log.status('Leaking %s' % s)

        pltgot = base + gotoff
        linkmap = d(pltgot + 4)

        status('linkmap')
        #Find named library
        nameaddr = d(linkmap + 4)
        name = s(nameaddr)
        while not lib in name:
            linkmap = d(linkmap + 12)
            if linkmap == 0:
                #No such library
                return None
            nameaddr = d(linkmap + 4)
            name = s(nameaddr)
        libbase = d(linkmap)
        dynamic = d(linkmap + 8)

        status('.gnu.hash, .strtab and .symtab offsets')
        #Find hashes, string table and symbol table
        hshtab = None
        strtab = None
        symtab = None
        while None in [hshtab, strtab, symtab]:
            tag = d(dynamic)
            if tag == 4:
                hshtab = d(dynamic + 4)
            elif tag == 5:
                strtab = d(dynamic + 4)
            elif tag == 6:
                symtab = d(dynamic + 4)
            dynamic += 8

        #Everything set up for resolving
        nbuckets = d(hshtab)
        bucketaddr = hshtab + 8
        chain = hshtab + 8 + nbuckets * 4

        status('hashmap')
        h = sysv_hash(symb) % nbuckets
        idx = leak.d(bucketaddr, h)
        while idx:
            sym = symtab + (idx * 16)
            symtype = b(sym + 12) & 0xf
            if symtype == 2:
                #Function type symbol
                name = s(strtab + d(sym))
                if name == symb:
                    #Bingo
                    pwn.log.succeeded()
                    return libbase + d(sym + 4)
            idx = leak.d(chain, idx)

        pwn.log.failed()
        return None

    def _lookup64 (self, symb, lib):
        base = self.base
        leak = self.leak
        gotoff = self.elf.sections['.got.plt']['addr']
        if base is None:
            pass
            # XXX: Read base address
            # else:
            #     pwn.log.die('Position independent ELF needs a base address')
        else:
            gotplt = base + gotoff

        pwn.log.waitfor('Resolving "%s"' % symb)

        def status(s):
            pwn.log.status('Leaking %s' % s)

        status('link_map')
        link_map = leak.q(gotplt, 1)

        status('%s load address' % lib)
        cur = link_map
        while True:
            addr = leak.q(cur + 8)
            name = leak.s(addr)
            if lib in name:
                break
            cur = leak.q(cur + 24)
        libbase = leak.q(cur)

        status('program headers offset')
        e_phoff = leak.q(libbase + 32)
        e_ph = libbase + e_phoff

        status('.dynamic section offset')
        cur = e_ph
        while True:
            typ = leak.d(cur)
            if typ == 2:
                break
            cur += 7 * 8

        dynoff = leak.q(cur + 16)
        dyn = libbase + dynoff

        status('.gnu.hash/.hash, .strtab and .symtab offsets')
        cur = dyn
        hshtag = None
        hshtab = None
        strtab = None
        symtab = None
        while None in [hshtab, strtab, symtab]:
            tag = leak.q(cur)
            if   tag == 4:
                hshtab = leak.q(cur, 1)
                hshtag = tag
            elif tag == 5:
                strtab = leak.q(cur, 1)
            elif tag == 6:
                symtab = leak.q(cur, 1)
            elif tag == 0x6ffffef5:
                hshtab = leak.q(cur, 1)
                hshtag = tag
            cur += 16

        # with glibc the pointers are relocated whereas with f.x. uclibc they
        # are not
        if libbase > strtab:
            strtab += libbase
            symtab += libbase
            hshtab += libbase

        if hshtag == 4:
            #SYSV hash
            pass
        else:
            status('.gnu.hash parms')
            nbuckets = leak.d(hshtab)
            symndx = leak.d(hshtab, 1)
            maskwords = leak.d(hshtab, 2)

            buckets = hshtab + 16 + 8 * maskwords
            chains = buckets + 4 * nbuckets

            status('hash chain index')
            hsh = gnu_hash(symb)
            bucket = hsh % nbuckets
            ndx = leak.d(buckets, bucket)
            chain = chains + 4 * (ndx - symndx)
            if ndx == 0:
                pwn.log.failed('Empty chain')
                return None
            status('hash chain')
            i = 0
            hsh &= ~1
            while True:
                hsh2 = leak.d(chain + (i * 4))
                if hsh == (hsh2 & ~1):
                    #Hash matches, but this may be a collision
                    #Check symbol name too.
                    sym = symtab + 24 * (ndx + i)
                    name = leak.s(strtab + leak.d(sym))
                    if name == symb:
                        break
                if hsh2 & 1:
                    pwn.log.failed('No hash')
                    return None
                i += 1
            status('symbol offset')
            offset = leak.q(sym, 1)
            pwn.log.succeeded()
            return offset + libbase
