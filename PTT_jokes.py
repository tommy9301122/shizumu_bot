import requests
from bs4 import BeautifulSoup
import time
import random

## 全域的function
def Access_and_GetHtml(url):
    try:
        response = requests.get(url)
        if response.status_code == 200:
            
            required_html = BeautifulSoup(response.text, 'html.parser')
            return required_html
        else:    
            print('请求错误状态码：', response.status_code)
            return "Error"    
    except Exception as e:
        print(e)
        return None

############################## Ptt Jokes ###########################


class PttJokes:

    def __init__(self,number):
        self.number = number

    def GetPttJoke(self,link):
        ## get html 

        pttjoke_html = Access_and_GetHtml(link) 

        if pttjoke_html!= "Error" or ptt_page != None:
            firstblock = pttjoke_html.select('.article-meta-value')
            author = firstblock[0].text
            topic= firstblock[2].text
            date = firstblock[3].text

            split_text = u'※ 發信站: 批踢踢實業坊(ptt.cc),'

            ## get main content
            initial_content = pttjoke_html.find(id="main-content").text
            content = initial_content.split(split_text)
            content = content[0].split(date)
            content = content[1].split('--')
            content = content[0].rstrip(" ")
            main_content = content.lstrip(" ")
            output_format = topic+""+'\n'+main_content+author+date+" From ptt"+"\n"

            return output_format
        else:
            return " "

    def PTT_page(self,page):



        ptt_header = "https://www.ptt.cc/bbs/joke/index"+str(page)+".html"
        ptt_page=Access_and_GetHtml(ptt_header)



        if ptt_page != "Error" or ptt_page !=None:
            
            title_and_link = ptt_page.select("div.title > a")
            
            post_url = list(map(lambda x:"https://www.ptt.cc"+x.get('href'), title_and_link))
            post_name = list(map(lambda x:x.text.rstrip(" "), title_and_link))

            ## not replies of other posts, returns True
            not_reply = list(map(lambda x:x[:2]!="Re", post_name))
            #print(not_reply)


            output_dict={post_name[index]:post_url[index] for index in range(len(not_reply)) if not_reply[index]==True}

            return output_dict
            
        else:
            return None
    
    def output(self):

        current_number_of_jokes = 0
        output = ""
        need_jokes = self.number

        while current_number_of_jokes < self.number:

            # Currently hard code to page 7775, need to change to adaption.
            random_page = random.randint(2,7775)
            page_output = self.PTT_page(random_page)
            
            ## number of jokes available
            jokes_in_this_page =len(page_output.keys())
            current_number_of_jokes += jokes_in_this_page

            if jokes_in_this_page > need_jokes:

                ##  sample enough needed jokes
                random_keys= random.sample(page_output.keys(),need_jokes)

                for rk in random_keys:
                    output += self.GetPttJoke(page_output[rk])

                break
            ## if jokes in this page is less than requested
            else:

                for normal in page_output.keys():
                    output += self.GetPttJoke(page_output[normal])

                need_jokes -= current_number_of_jokes
        ## should output text of required number of jokes
        return output



#ptt = PttJokes(1)
#print(ptt.output())
# test succeed