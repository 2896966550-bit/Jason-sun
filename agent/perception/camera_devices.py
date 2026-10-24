"""DirectShow monikers in the same category/order used by CAP_DSHOW on Windows."""
import ctypes as C
import os
import uuid


def enumerate_devices():
    if os.name != 'nt':
        raise RuntimeError('本工具目前只支持 Windows DirectShow 设备身份')
    class GUID(C.Structure):
        _fields_=[('a',C.c_uint32),('b',C.c_uint16),('c',C.c_uint16),('d',C.c_ubyte*8)]
    def guid(s):return GUID.from_buffer_copy(uuid.UUID(s).bytes_le)
    def method(obj,index,types):
        address=C.cast(obj,C.POINTER(C.POINTER(C.c_void_p))).contents[index]
        return C.WINFUNCTYPE(C.c_long,C.c_void_p,*types)(address)
    def release(obj):
        if obj:method(obj,2,[])(obj)
    ole=C.OleDLL('ole32')
    ole.CoInitializeEx.argtypes=[C.c_void_p,C.c_uint32]
    ole.CoCreateInstance.argtypes=[C.POINTER(GUID),C.c_void_p,C.c_uint32,C.POINTER(GUID),C.POINTER(C.c_void_p)]
    ole.CoTaskMemFree.argtypes=[C.c_void_p]
    initialized=ole.CoInitializeEx(None,0) in (0,1)
    dev=C.c_void_p(); enum=C.c_void_p(); result=[]
    try:
        cls=guid('62BE5D10-60EB-11D0-BD3B-00A0C911CE86')
        iid=guid('29840822-5B84-11D0-BD3B-00A0C911CE86')
        hr=ole.CoCreateInstance(C.byref(cls),None,1,C.byref(iid),C.byref(dev))
        if hr<0:raise RuntimeError('DirectShow 设备枚举器不可用')
        category=guid('860BB310-5D01-11D0-BD3B-00A0C911CE86')
        hr=method(dev,3,[C.POINTER(GUID),C.POINTER(C.c_void_p),C.c_uint32])(dev,C.byref(category),C.byref(enum),0)
        if hr==1:return []
        if hr<0:raise RuntimeError('DirectShow 枚举失败')
        while True:
            moniker=C.c_void_p();fetched=C.c_uint32()
            hr=method(enum,3,[C.c_uint32,C.POINTER(C.c_void_p),C.POINTER(C.c_uint32)])(enum,1,C.byref(moniker),C.byref(fetched))
            if hr!=0 or fetched.value!=1:break
            name=C.c_void_p()
            try:
                hr=method(moniker,20,[C.c_void_p,C.c_void_p,C.POINTER(C.c_void_p)])(moniker,None,None,C.byref(name))
                if hr<0:raise RuntimeError('相机缺少可读取的设备标识')
                result.append({'index':len(result),'device_id':C.wstring_at(name),'backend':'DSHOW'})
            finally:
                if name:ole.CoTaskMemFree(name)
                release(moniker)
        return result
    finally:
        release(enum);release(dev)
        if initialized:ole.CoUninitialize()


def resolve_device(device_id, inventory):
    matches=[d for d in inventory if d['device_id']==device_id]
    if len(matches)!=1:raise ValueError('设备未连接或身份不唯一，请重新核对左右设备')
    return matches[0]['index']
