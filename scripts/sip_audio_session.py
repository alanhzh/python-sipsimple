#!/usr/bin/env python

import sys
import traceback
import string
import socket
import os
import atexit
import select
import termios
import signal
import datetime
import random
from thread import start_new_thread, allocate_lock
from threading import Thread, Timer
from Queue import Queue
from optparse import OptionParser, OptionValueError
from time import sleep, time
from application.process import process
from application.configuration import *
from pypjua import *
from pypjua.clients import enrollment
from pypjua.clients.log import Logger

from pypjua.clients.lookup import *
from pypjua.clients.clientconfig import get_path
from pypjua.clients import format_cmdline_uri

class GeneralConfig(ConfigSection):
    _datatypes = {"local_ip": datatypes.IPAddress, "sip_transports": datatypes.StringList, "trace_pjsip": datatypes.Boolean, "trace_sip": datatypes.Boolean}
    local_ip = None
    sip_local_udp_port = 0
    sip_local_tcp_port = 0
    sip_local_tls_port = 0
    sip_transports = ["tls", "tcp", "udp"]
    trace_pjsip = False
    trace_sip = False
    history_directory = '~/.sipclient/history'
    log_directory = '~/.sipclient/log'


class AccountConfig(ConfigSection):
    _datatypes = {"sip_address": str, "password": str, "display_name": str, "outbound_proxy": OutboundProxy, "use_ice": datatypes.Boolean, "use_stun_for_ice": datatypes.Boolean, "stun_servers": datatypes.StringList}
    sip_address = None
    password = None
    display_name = None
    outbound_proxy = None
    use_ice = False
    use_stun_for_ice = False
    stun_servers = []


class SRTPOptions(dict):
    def __new__(typ, value):
        value_lower = value.lower()
        if value_lower == "disabled":
            return dict(use_srtp=False, srtp_forced=False)
        elif value_lower == "optional":
            return dict(use_srtp=True, srtp_forced=False)
        elif value_lower == "mandatory":
            return dict(use_srtp=True, srtp_forced=True)
        else:
            raise ValueError('Unknown SRTP option: "%s"' % value)


class AudioConfig(ConfigSection):
    _datatypes = {"sample_rate": int, "echo_cancellation_tail_length": int,"codec_list": datatypes.StringList, "disable_sound": datatypes.Boolean, "encryption": SRTPOptions}
    sample_rate = 32
    echo_cancellation_tail_length = 50
    codec_list = ["speex", "g711", "ilbc", "gsm", "g722"]
    disable_sound = False
    encryption = dict(use_srtp=True, srtp_forced=False)


process._system_config_directory = os.path.expanduser("~/.sipclient")
enrollment.verify_account_config()
configuration = ConfigFile("config.ini")
configuration.read_settings("Audio", AudioConfig)
configuration.read_settings("General", GeneralConfig)

queue = Queue()
packet_count = 0
start_time = None
old = None
user_quit = True
lock = allocate_lock()
logger = None
return_code = 1

def termios_restore():
    global old
    if old is not None:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)

def getchar():
    global old
    fd = sys.stdin.fileno()
    if os.isatty(fd):
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] = new[3] & ~termios.ICANON & ~termios.ECHO
        new[6][termios.VMIN] = '\000'
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
            if select.select([fd], [], [], None)[0]:
                return sys.stdin.read(10)
        finally:
            termios_restore()
    else:
        return os.read(fd, 10)

def event_handler(event_name, **kwargs):
    global start_time, packet_count, queue, do_trace_pjsip, logger
    if event_name == "siptrace":
        logger.log(event_name, **kwargs)
    elif event_name != "log":
        queue.put(("pypjua_event", (event_name, kwargs)))
    elif do_trace_pjsip:
        queue.put(("print", "%(timestamp)s (%(level)d) %(sender)14s: %(message)s" % kwargs))

class RingingThread(Thread):

    def __init__(self, inbound):
        self.inbound = inbound
        self.stopping = False
        Thread.__init__(self)
        self.setDaemon(True)
        self.start()

    def stop(self):
        self.stopping = True

    def run(self):
        global queue
        while True:
            if self.stopping:
                return
            if self.inbound:
                queue.put(("play_wav", "ring_inbound.wav"))
            else:
                queue.put(("play_wav", "ring_outbound.wav"))
            sleep(5)

def print_control_keys():
    print "Available control keys:"
    print "  h: hang-up the active session"
    print "  r: toggle audio recording"
    print "  <> : adjust echo cancellation"
    print "  SPACE: hold/on-hold"
    print "  Ctrl-d: quit the program"

def read_queue(e, username, domain, password, display_name, route, target_uri, trace_sip, ec_tail_length, sample_rate, codecs, do_trace_pjsip, use_bonjour, stun_servers, transport, auto_hangup):
    global user_quit, lock, queue, return_code
    lock.acquire()
    inv = None
    audio = None
    ringer = None
    printed = False
    rec_file = None
    want_quit = target_uri is not None
    other_user_agent = None
    on_hold = False
    session_start_time = None
    try:
        if not use_bonjour:
            sip_uri = SIPURI(user=username, host=domain, display=display_name)
            credentials = Credentials(sip_uri, password)
            if len(stun_servers) > 0:
                e.detect_nat_type(*stun_servers[0])
        if target_uri is None:
            if use_bonjour:
                print "Using bonjour"
                print "Listening on local interface %s:%d" % (e.local_ip, e.local_udp_port)
                print_control_keys()
                print 'Waiting for incoming SIP session requests...'
            else:
                reg = Registration(credentials, route=route)
                print 'Registering "%s" at %s:%d' % (credentials.uri, route.host, route.port)
                reg.register()
        else:
            inv = Invitation(credentials, target_uri, route=route)
            print "Call from %s to %s through proxy %s:%s:%d" % (inv.caller_uri, inv.callee_uri, route.transport, route.host, route.port)
            audio = AudioTransport(transport)
            inv.set_offered_local_sdp(SDPSession(audio.transport.local_rtp_address, connection=SDPConnection(audio.transport.local_rtp_address), media=[audio.get_local_media(True)]))
            inv.send_invite()
            print_control_keys()
        while True:
            command, data = queue.get()
            if command == "print":
                print data
            if command == "pypjua_event":
                event_name, args = data
                if event_name == "Registration_state":
                    if args["state"] == "registered":
                        if not printed:
                            print "REGISTER was successful"
                            print "Contact: %s (expires in %d seconds)" % (args["contact_uri"], args["expires"])
                            print_control_keys()
                            print "Waiting for incoming session..."
                            printed = True
                    elif args["state"] == "unregistered":
                        if "code" in args and args["code"] / 100 != 2:
                            print "Unregistered: %(code)d %(reason)s" % args
                        elif inv is None:
                            return_code = 0
                        user_quit = False
                        command = "quit"
                elif event_name == "Invitation_sdp":
                    if args["obj"] is inv:
                        if args["succeeded"]:
                            if not audio.is_started:
                                if ringer is not None:
                                    ringer.stop()
                                    ringer = None
                                session_start_time = time()
                                audio.start(args["local_sdp"], args["remote_sdp"], 0)
                                e.connect_audio_transport(audio)
                                print 'Media negotiation done, using "%s" codec at %dHz' % (audio.codec, audio.sample_rate)
                                print "Audio RTP endpoints %s:%d <-> %s:%d" % (audio.transport.local_rtp_address, audio.transport.local_rtp_port, audio.transport.remote_rtp_address_sdp, audio.transport.remote_rtp_port_sdp)
                                return_code = 0
                                if auto_hangup is not None:
                                    Timer(auto_hangup, lambda: queue.put(("eof", None))).start()
                                if audio.transport.srtp_active:
                                    print "RTP audio stream is encrypted"
                            else:
                                audio.update_direction(inv.get_active_local_sdp().media[0].get_direction())
                        else:
                            print "SDP negotation failed: %s" % args["error"]
                elif event_name == "Invitation_state":
                    if args["state"] == args["prev_state"] and args["state"] != "EARLY":
                        print "SAME STATE"
                        print args
                        data, args = None, None
                        continue
                    if args["state"] == "EARLY":
                        if "code" in args and args["code"] == 180:
                            if ringer is None:
                                print "Ringing..."
                                ringer = RingingThread(target_uri is None)
                    elif args["state"] == "CONNECTING":
                        if "headers" in args and "User-Agent" in args["headers"]:
                            other_user_agent = args["headers"].get("User-Agent")
                    elif args["state"] == "INCOMING":
                        print "Incoming session..."
                        if inv is None:
                            remote_sdp = args["obj"].get_offered_remote_sdp()
                            if remote_sdp is not None and len(remote_sdp.media) == 1 and remote_sdp.media[0].media == "audio":
                                inv = args["obj"]
                                other_user_agent = args["headers"].get("User-Agent")
                                if ringer is None:
                                    ringer = RingingThread(True)
                                inv.respond_to_invite_provisionally()
                                print 'Incoming audio session from "%s", do you want to accept? (y/n)' % str(inv.caller_uri)
                            else:
                                print "Not an audio call, rejecting."
                                args["obj"].disconnect()
                        else:
                            print "Rejecting."
                            args["obj"].disconnect()
                    elif args["prev_state"] == "CONNECTING" and args["state"] == "CONFIRMED":
                        if other_user_agent is not None:
                            print 'Remote SIP User Agent is "%s"' % other_user_agent
                    elif args["state"] == "REINVITED":
                        # Just assume the call got placed on hold for now...
                        prev_remote_direction = inv.get_active_remote_sdp().media[0].get_direction()
                        remote_direction = inv.get_offered_remote_sdp().media[0].get_direction()
                        if "recv" in prev_remote_direction and "recv" not in remote_direction:
                            print "Remote party is placing us on hold"
                        elif "recv" not in prev_remote_direction and "recv" in remote_direction:
                            print "Remote party is taking us out of hold"
                        local_sdp = inv.get_active_local_sdp()
                        local_sdp.version += 1
                        local_sdp.media[0] = audio.get_local_media(False)
                        inv.set_offered_local_sdp(local_sdp)
                        inv.respond_to_reinvite()
                    elif args["state"] == "DISCONNECTED":
                        if args["obj"] is inv:
                            if rec_file is not None:
                                rec_file.stop()
                                print 'Stopped recording audio to "%s"' % rec_file.file_name
                                rec_file = None
                            if ringer is not None:
                                ringer.stop()
                                ringer = None
                            if args["prev_state"] == "DISCONNECTING":
                                disc_msg = "Session ended by local user"
                            elif args["prev_state"] in ["CALLING", "EARLY"]:
                                if "headers" in args:
                                    if "Server" in args["headers"]:
                                        print 'Remote SIP server is "%s"' % args["headers"]["Server"]
                                    elif "User-Agent" in args["headers"]:
                                        print 'Remote SIP User Agent is "%s"' % args["headers"]["User-Agent"]
                                disc_msg = "Session could not be established"
                            else:
                                disc_msg = 'Session ended by "%s"' % inv.remote_uri
                            if "code" in args and args["code"] / 100 != 2:
                                print "%s: %d %s" % (disc_msg, args["code"], args["reason"])
                                if args["code"] == 408 and args["prev_state"] == "CONNECTING":
                                    print "Session failed because ACK was never received"
                                if args["code"] in [301, 302]:
                                    print 'Received redirect request to "%s"' % args["headers"]["Contact"]
                                    return_code = 0
                            else:
                                print disc_msg
                            if session_start_time is not None:
                                duration = time() - session_start_time
                                print "Session duration was %d minutes, %d seconds" % (duration / 60, duration % 60)
                                session_start_time = None
                            if want_quit:
                                command = "unregister"
                            else:
                                if audio is not None:
                                    audio.stop()
                                audio = None
                                inv = None
                elif event_name == "detect_nat_type":
                    if args["succeeded"]:
                        print "Detected NAT type: %s" % args["nat_type"]
            if command == "user_input":
                if inv is not None:
                    data = data[0]
                    if data.lower() == "h":
                        command = "end"
                        want_quit = target_uri is not None
                    elif data in "0123456789*#ABCD" and audio is not None and audio.is_active:
                        audio.send_dtmf(data)
                    elif data.lower() == "r":
                        if rec_file is None:
                            src = '%s@%s' % (inv.caller_uri.user, inv.caller_uri.host)
                            dst = '%s@%s' % (inv.callee_uri.user, inv.callee_uri.host)
                            dir = os.path.join(os.path.expanduser(GeneralConfig.history_directory), '%s@%s' % (username, domain))
                            try:
                                file_name = os.path.join(dir, '%s-%s-%s.wav' % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), src, dst))
                                rec_file = e.rec_wav_file(file_name)
                                print 'Recording audio to "%s"' % rec_file.file_name
                            except OSError, e:
                                print "Error while trying to record file: %s"
                        else:
                            rec_file.stop()
                            print 'Stopped recording audio to "%s"' % rec_file.file_name
                            rec_file = None
                    elif data == " ":
                        if inv.state == "CONFIRMED":
                            if not on_hold:
                                on_hold = True
                                print "Placing call on hold"
                                if "send" in audio.direction:
                                    new_direction = "sendonly"
                                else:
                                    new_direction = "inactive"
                                e.disconnect_audio_transport(audio)
                            else:
                                on_hold = False
                                print "Taking call out of hold"
                                if "send" in audio.direction:
                                    new_direction = "sendrecv"
                                else:
                                    new_direction = "recvonly"
                                e.connect_audio_transport(audio)
                            local_sdp = inv.get_active_local_sdp()
                            local_sdp.version += 1
                            local_sdp.media[0] = audio.get_local_media(True, new_direction)
                            inv.set_offered_local_sdp(local_sdp)
                            inv.send_reinvite()
                    elif inv.state in ["INCOMING", "EARLY"] and target_uri is None:
                        if data.lower() == "n":
                            command = "end"
                            want_quit = False
                        elif data.lower() == "y":
                            remote_sdp = inv.get_offered_remote_sdp()
                            audio = AudioTransport(transport, remote_sdp, 0)
                            inv.set_offered_local_sdp(SDPSession(audio.transport.local_rtp_address, connection=SDPConnection(audio.transport.local_rtp_address), media=[audio.get_local_media(False)], start_time=remote_sdp.start_time, stop_time=remote_sdp.stop_time))
                            inv.accept_invite()
                if data in ",<":
                    if ec_tail_length > 0:
                        ec_tail_length = max(0, ec_tail_length - 10)
                        e.auto_set_sound_devices(ec_tail_length)
                    print "Set echo cancellation tail length to %d ms" % ec_tail_length
                elif data in ".>":
                    if ec_tail_length < 500:
                        ec_tail_length = min(500, ec_tail_length + 10)
                        e.auto_set_sound_devices(ec_tail_length)
                    print "Set echo cancellation tail length to %d ms" % ec_tail_length
            if command == "play_wav":
                e.play_wav_file(get_path(data))
            if command == "eof":
                command = "end"
                want_quit = True
            if command == "end":
                try:
                    inv.disconnect()
                except:
                    command = "unregister"
            if command == "unregister":
                if target_uri is None and not use_bonjour:
                    reg.unregister()
                else:
                    user_quit = False
                    command = "quit"
            if command == "quit":
                break
            data, args = None, None
    except:
        user_quit = False
        traceback.print_exc()
    finally:
        e.stop()
        logger.stop()
        if not user_quit:
            os.kill(os.getpid(), signal.SIGINT)
        lock.release()

def do_invite(**kwargs):
    global user_quit, lock, queue, do_trace_pjsip, logger
    ctrl_d_pressed = False
    do_trace_pjsip = kwargs["do_trace_pjsip"]
    outbound_proxy = kwargs.pop("outbound_proxy")
    kwargs["stun_servers"] = lookup_service_for_sip_uri(SIPURI(host=kwargs["domain"]), "stun")
    if kwargs["use_bonjour"]:
        kwargs["route"] = None
    else:
        if outbound_proxy is None:
            routes = lookup_routes_for_sip_uri(SIPURI(host=kwargs["domain"]), kwargs.pop("sip_transports"))
        else:
            routes = lookup_routes_for_sip_uri(outbound_proxy, kwargs.pop("sip_transports"))
        # Only try the first Route for now
        try:
            kwargs["route"] = routes[0]
        except IndexError:
            raise RuntimeError("No route found to SIP proxy")
    
    logger = Logger(AccountConfig, GeneralConfig.log_directory, trace_sip=kwargs['trace_sip'])
    if kwargs['trace_sip']:
        print "Logging SIP trace to file '%s'" % logger._siptrace_filename
    
    e = Engine(event_handler, trace_sip=True, codecs=kwargs["codecs"], ec_tail_length=kwargs["ec_tail_length"], sample_rate=kwargs["sample_rate"], local_ip=kwargs["local_ip"], local_udp_port=kwargs.pop("local_udp_port"), local_tcp_port=kwargs.pop("local_tcp_port"), local_tls_port=kwargs.pop("local_tls_port"))
    e.start(not kwargs.pop("disable_sound"))
    if kwargs["target_uri"] is not None:
        kwargs["target_uri"] = e.parse_sip_uri(kwargs["target_uri"])
    transport_kwargs = AudioConfig.encryption.copy()
    transport_kwargs["use_ice"] = AccountConfig.use_ice
    wait_for_stun = False
    if AccountConfig.use_stun_for_ice:
        if len(AccountConfig.stun_servers) > 0:
            wait_for_stun = True
            try:
                random_stun = random.choice(AccountConfig.stun_servers)
                transport_kwargs["ice_stun_address"], ice_stun_port = random_stun.split(":")
            except:
                transport_kwargs["ice_stun_address"] = random_stun
                transport_kwargs["ice_stun_port"] = 3478
            else:
                transport_kwargs["ice_stun_port"] = int(ice_stun_port)
        else:
            if len(kwargs["stun_servers"]) > 0:
                wait_for_stun = True
                transport_kwargs["ice_stun_address"], transport_kwargs["ice_stun_port"] = random.choice(kwargs["stun_servers"])
    kwargs["transport"] = RTPTransport(kwargs.pop("local_ip"), **transport_kwargs)
    if wait_for_stun:
        print "Waiting for STUN response for ICE from %s:%d" % (transport_kwargs["ice_stun_address"], transport_kwargs["ice_stun_port"])
        while True:
            command, data = queue.get()
            if command == "print":
                print data
            elif command == "pypjua_event":
                event_name, args = data
                if event_name == "RTPTransport_init":
                    if args["succeeded"]:
                        break
                    else:
                        raise RuntimeError("STUN request failed")
    start_new_thread(read_queue, (e,), kwargs)
    atexit.register(termios_restore)
    try:
        while True:
            char = getchar()
            if char == "\x04":
                if not ctrl_d_pressed:
                    queue.put(("eof", None))
                    ctrl_d_pressed = True
            else:
                queue.put(("user_input", char))
    except KeyboardInterrupt:
        if user_quit:
            print "Ctrl+C pressed, exiting instantly!"
            queue.put(("quit", True))
        lock.acquire()
        return

def parse_outbound_proxy(option, opt_str, value, parser):
    try:
        parser.values.outbound_proxy = OutboundProxy(value)
    except ValueError, e:
        raise OptionValueError(e.message)

def parse_auto_hangup(option, opt_str, value, parser):
    try:
        value = parser.rargs[0]
    except IndexError:
        value = 0
    else:
        if value == "" or value[0] == '-':
            value = 0
        else:
            try:
                value = int(value)
            except ValueError:
                value = 0
            else:
                del parser.rargs[0]
    parser.values.auto_hangup = value

def split_codec_list(option, opt_str, value, parser):
    parser.values.codecs = value.split(",")

def parse_options():
    retval = {}
    description = "This script can sit idle waiting for an incoming audio call, or perform an outgoing audio call to the target SIP account. The program will close the session and quit when Ctrl+D is pressed."
    usage = "%prog [options] [target-user@target-domain.com]"
    parser = OptionParser(usage=usage, description=description)
    parser.print_usage = parser.print_help
    parser.add_option("-a", "--account-name", type="string", dest="account_name", help="The account name from which to read account settings. Corresponds to section Account_NAME in the configuration file. If not supplied, the section Account will be read.", metavar="NAME")
    parser.add_option("--sip-address", type="string", dest="sip_address", help="SIP address of the user in the form user@domain")
    parser.add_option("-p", "--password", type="string", dest="password", help="Password to use to authenticate the local account. This overrides the setting from the config file.")
    parser.add_option("-n", "--display-name", type="string", dest="display_name", help="Display name to use for the local account. This overrides the setting from the config file.")
    parser.add_option("-o", "--outbound-proxy", type="string", action="callback", callback=parse_outbound_proxy, help="Outbound SIP proxy to use. By default a lookup of the domain is performed based on SRV and A records. This overrides the setting from the config file.", metavar="IP[:PORT]")
    parser.add_option("-s", "--trace-sip", action="store_true", dest="trace_sip", help="Dump the raw contents of incoming and outgoing SIP messages (disabled by default).")
    parser.add_option("-t", "--ec-tail-length", type="int", dest="ec_tail_length", help='Echo cancellation tail length in ms, setting this to 0 will disable echo cancellation. Default is 50 ms.')
    parser.add_option("-r", "--sample-rate", type="int", dest="sample_rate", help='Sample rate in kHz, should be one of 8, 16 or 32kHz. Default is 32kHz.')
    parser.add_option("-c", "--codecs", type="string", action="callback", callback=split_codec_list, help='Comma separated list of codecs to be used. Default is "speex,g711,ilbc,gsm,g722".')
    parser.add_option("-S", "--disable-sound", action="store_true", dest="disable_sound", help="Do not initialize the soundcard (by default the soundcard is enabled).")
    parser.add_option("-j", "--trace-pjsip", action="store_true", dest="do_trace_pjsip", help="Print PJSIP logging output (disabled by default).")
    parser.add_option("--auto-hangup", action="callback", callback=parse_auto_hangup, help="Interval after which to hangup an on-going call (applies only to outgoing calls, disabled by default). If the option is specified but the interval is not, it defaults to 0 (hangup the call as soon as it connects).", metavar="[INTERVAL]")
    options, args = parser.parse_args()

    retval["use_bonjour"] = options.account_name == "bonjour"
    if not retval["use_bonjour"]:
        if options.account_name is None:
            account_section = "Account"
        else:
            account_section = "Account_%s" % options.account_name
        if account_section not in configuration.parser.sections():
            raise RuntimeError("There is no account section named '%s' in the configuration file" % account_section)
        configuration.read_settings(account_section, AccountConfig)
    default_options = dict(outbound_proxy=AccountConfig.outbound_proxy, sip_address=AccountConfig.sip_address, password=AccountConfig.password, display_name=AccountConfig.display_name, trace_sip=GeneralConfig.trace_sip, ec_tail_length=AudioConfig.echo_cancellation_tail_length, sample_rate=AudioConfig.sample_rate, codecs=AudioConfig.codec_list, disable_sound=AudioConfig.disable_sound, do_trace_pjsip=GeneralConfig.trace_pjsip, local_ip=GeneralConfig.local_ip, local_udp_port=GeneralConfig.sip_local_udp_port, local_tcp_port=GeneralConfig.sip_local_tcp_port, local_tls_port=GeneralConfig.sip_local_tls_port, sip_transports=GeneralConfig.sip_transports, auto_hangup=None)
    options._update_loose(dict((name, value) for name, value in default_options.items() if getattr(options, name, None) is None))

    for transport in set(["tls", "tcp", "udp"]) - set(options.sip_transports):
        setattr(options, "local_%s_port" % transport, None)
    if not retval["use_bonjour"]:
        if not all([options.sip_address, options.password]):
            raise RuntimeError("No complete set of SIP credentials specified in config file and on commandline.")
    for attr in default_options:
        retval[attr] = getattr(options, attr)
    try:
        if retval["use_bonjour"]:
            retval["username"], retval["domain"] = None, None
        else:
            retval["username"], retval["domain"] = options.sip_address.split("@")
    except ValueError:
        raise RuntimeError("Invalid value for sip_address: %s" % options.sip_address)
    else:
        del retval["sip_address"]
    if args:
        retval["target_uri"] = format_cmdline_uri(args[0], retval["domain"])
    else:
        retval["target_uri"] = None
    accounts = [(acc == 'Account') and 'default' or "'%s'" % acc[8:] for acc in configuration.parser.sections() if acc.startswith('Account')]
    accounts.sort()
    print "Accounts available: %s" % ', '.join(accounts)
    if options.account_name is None:
        print "Using default account: %s" % options.sip_address
    else:
        if not retval["use_bonjour"]:
            print "Using account '%s': %s" % (options.account_name, options.sip_address)
    return retval

def main():
    do_invite(**parse_options())

if __name__ == "__main__":
    try:
        main()
    except RuntimeError, e:
        print "Error: %s" % str(e)
        sys.exit(1)
    except PyPJUAError, e:
        print "Error: %s" % str(e)
        sys.exit(1)
    sys.exit(return_code)
